"""The API connection's Test of an OpenAI-format connection sends a request shaped as runs send TCS Prover's team
tools (ui/server.py probe_team_tools(), after the first real coordinator trial of 14 Nov 2026: the Test's one plain
tool proved nothing of the shape a coordinator's requests have, so a proxy that drops or flattens namespaced tools
went unseen).

Codex 0.160 sends the team tools (as captured from the real Codex against a scripted model server) as a namespace
"mcp__tcs" of functions: in the request's tools for a model it does not run as Responses Lite, and for a Lite model
(gpt-6.1-sol and the other names of its catalog) in an additional_tools input item, with reasoning context
"all_turns"; the model must call a tool back with its namespace, or Codex refuses the call ("unsupported call"). The
Test runs with the connection's first Lite model, else its first, and passes only when the call comes back with its
namespace. An answer that says nothing about the team tools leaves them not checked (ok None): no answer, a status
that may pass (408, 425, 429, 5xx: CLIProxyAPI's credentials "cooling down"), a refused key or model (401, 403, 404),
and another refusal that the same request without the team tools gets as well. Offline: a local endpoint plays the
connection.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers
from ui import server

KEY = "sk-team-tool-test-0123456789"


class Endpoint:
    """A Responses endpoint on 127.0.0.1: ``models`` for GET .../models; ``answer(body)`` -> (status, events, a JSON
    body or raw bytes) for POST .../responses."""

    def __init__(self, models, answer):
        self.models, self.answer, self.requests = models, answer, []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def send(self, status, data, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send(200, json.dumps({"object": "list", "data": [{"id": name} for name in endpoint.models]})
                          .encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                endpoint.requests.append({"path": self.path, "body": body,
                                          "authorization": self.headers.get("Authorization")})
                status, value = endpoint.answer(body)
                if isinstance(value, list):
                    self.send(status, "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                                              for event in value).encode(), "text/event-stream")
                elif isinstance(value, bytes):
                    self.send(status, value, "text/plain")
                else:
                    self.send(status, json.dumps(value).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def team_requests(self):
        return [request["body"] for request in self.requests if request["path"].endswith("/responses")]


def call(name, namespace=None):
    """A streamed answer that calls ``name`` (in ``namespace``)."""

    item = {"type": "function_call", "call_id": "call_1", "name": name, "arguments": '{"ok": true}',
            **({"namespace": namespace} if namespace else {})}
    return [{"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": "resp_1"}}]


TEXT = [{"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_text.delta", "delta": '{"ok": true}'},
        {"type": "response.completed", "response": {"id": "resp_1"}}]


def with_tools(body):
    """Whether a request carries the team tools (in its tools, or in an additional_tools item)."""

    return "tools" in body or any(item.get("type") == "additional_tools" for item in body.get("input", []))


def with_tools_refused(status, value, plain=TEXT):
    """An endpoint's answer that refuses a request with the team tools (``status``, ``value``) and answers ``plain``
    to the same request without them."""

    return lambda body: (status, value) if with_tools(body) else (200, plain)


class ProbeTests(unittest.TestCase):
    def endpoint(self, models, answer):
        endpoint = Endpoint(models, answer)
        self.addCleanup(endpoint.close)
        return endpoint

    @staticmethod
    def entry(endpoint):
        return {"format": "openai", "baseUrl": endpoint.base, "requestSource": "TCS Prover", "apiKey": KEY,
                "maxOutputTokens": None}

    def test_a_lite_model_gets_the_tools_in_an_additional_tools_item_and_passes_with_the_namespace(self):
        endpoint = self.endpoint(["claude-gw", "gpt-6.1-sol"], lambda body: (200, call("report_result", "mcp__tcs")))
        result = server.probe_team_tools(self.entry(endpoint), endpoint.models, effort="high")
        self.assertEqual((result["ok"], result["model"], result["form"]), (True, "gpt-6.1-sol", "additional_tools"))
        self.assertEqual(result["message"], "Team tools reach the model: gpt-6.1-sol called mcp__tcs.report_result with "
                                            "its namespace (the tools went in an additional_tools item, as Codex "
                                            "sends a Lite model's tools).")
        body, = endpoint.team_requests()
        # As Codex 0.160 sends a Lite model's requests: no tools at the top; the namespace in additional_tools.
        self.assertNotIn("tools", body)
        extra, message = body["input"]
        self.assertEqual((extra["type"], extra["role"]), ("additional_tools", "developer"))
        namespace, = extra["tools"]
        self.assertEqual((namespace["type"], namespace["name"], namespace["description"]),
                         ("namespace", "mcp__tcs", "Tools in the mcp__tcs namespace."))
        tool, = namespace["tools"]
        self.assertEqual((tool["type"], tool["name"], tool["strict"]), ("function", "report_result", False))
        self.assertEqual(message["role"], "user")
        self.assertEqual(body["reasoning"], {"effort": "high", "summary": server.DEFAULT_REASONING_SUMMARY,
                                             "context": "all_turns"})
        self.assertEqual((body["stream"], body["store"], body["include"], body["parallel_tool_calls"]),
                         (True, False, ["reasoning.encrypted_content"], False))
        self.assertEqual(endpoint.requests[0]["authorization"], f"Bearer {KEY}")

    def test_another_model_gets_the_namespace_in_the_request_s_tools(self):
        endpoint = self.endpoint(["lab-model"], lambda body: (200, call("report_result", "mcp__tcs")))
        result = server.probe_team_tools(self.entry(endpoint), endpoint.models)
        self.assertEqual((result["ok"], result["model"], result["form"]), (True, "lab-model", "tools"))
        body, = endpoint.team_requests()
        namespace, = body["tools"]
        self.assertEqual((namespace["type"], namespace["name"]), ("namespace", "mcp__tcs"))
        self.assertEqual([item["type"] for item in body["input"]], ["message"])
        self.assertNotIn("context", body["reasoning"])
        self.assertIn("as Codex sends them to this model", result["message"])

    def test_a_call_without_its_namespace_or_no_call_fails_and_says_why(self):
        for answer, expected in (
                (call("mcp__tcs__report_result"), "gpt-6.1-sol called mcp__tcs__report_result without the namespace "
                 "mcp__tcs: Codex refuses such a call (\"unsupported call\"), so a coordinator on this connection "
                 "could not use its team."),
                (call("report_result"), "gpt-6.1-sol called report_result without the namespace mcp__tcs"),
                (TEXT, "gpt-6.1-sol did not call mcp__tcs.report_result (the tools went in an additional_tools item, "
                 "as Codex sends a Lite model's tools): the endpoint may not pass namespaced tools on, and a "
                 "coordinator on this connection may not reach its team."),
                ([{"type": "response.failed", "response": {"error": {"message": "upstream busy"}}}],
                 "answered the request with team tools with an error: upstream busy")):
            endpoint = self.endpoint(["gpt-6.1-sol"], lambda body, answer=answer: (200, answer))
            with self.subTest(expected=expected):
                result = server.probe_team_tools(self.entry(endpoint), endpoint.models)
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["message"])

    def test_a_refused_request_says_so_in_the_endpoint_s_words_without_the_key(self):
        endpoint = self.endpoint(["gpt-6.1-sol"], with_tools_refused(400, {"error": {
            "message": f"Unknown input item type 'additional_tools' (key {KEY}).", "type": "invalid_request_error"}}))
        result = server.probe_team_tools(self.entry(endpoint), endpoint.models)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "The endpoint refused the request with team tools for gpt-6.1-sol, sent in "
                                            "an additional_tools item (HTTP 400: Unknown input item type "
                                            "'additional_tools' (key [redacted]).), but answers the same request "
                                            "without them. A coordinator on this connection could not reach its team.")
        self.assertNotIn(KEY, result["message"])
        self.assertIsNone(server.probe_team_tools(self.entry(endpoint), []))

    def test_a_refusal_s_message_ends_with_a_full_stop_within_what_a_connection_keeps(self):
        # The service's words in parentheses: words without a full stop of their own never run into the next
        # sentence, and long ones are clipped so that the message keeps its end (a connection keeps 300 characters).
        tail = ", but answers the same request without them. A coordinator on this connection could not reach its team."
        for words, expected in (
                ("Unsupported parameter: 'tools'", "The endpoint refused the request with team tools for lab-model, "
                 f"sent in the request's tools (HTTP 400: Unsupported parameter: 'tools'){tail}"),
                ("", "The endpoint refused the request with team tools for lab-model, sent in the request's tools "
                 f"(HTTP 400){tail}")):
            endpoint = self.endpoint(["lab-model"], with_tools_refused(
                400, {"error": {"message": words, "type": "invalid_request_error"}} if words else b""))
            with self.subTest(words=words):
                self.assertEqual(server.probe_team_tools(self.entry(endpoint), endpoint.models)["message"], expected)
        endpoint = self.endpoint(["lab-model"], with_tools_refused(400, {"error": {"message": "Unsupported tool. " * 40}}))
        message = server.probe_team_tools(self.entry(endpoint), endpoint.models)["message"]
        self.assertEqual(len(message), providers.MAX_TEST_MESSAGE_CHARS)
        self.assertIn("(HTTP 400: Unsupported tool. Unsupported tool.", message)
        self.assertTrue(message.endswith(f"…){tail}"))

    def test_an_answer_that_says_nothing_about_the_team_tools_leaves_them_not_checked(self):
        # A status that may pass (CLIProxyAPI's 429 "cooling down", a 503, a 500, a timeout of the upstream) and a
        # refused key or model (a model of the list that this key cannot use) claim nothing about the team tools:
        # not checked (ok None), with what came back, and no second request.
        cases = (
            (429, "All credentials for model gpt-6.1-sol are cooling down", "which may pass. Test again later."),
            (503, "auth_unavailable", "which may pass. Test again later."),
            (500, "Internal error", "which may pass. Test again later."),
            (408, "Upstream timed out", "which may pass. Test again later."),
            (404, "The model `gpt-6.1-sol` does not exist or you do not have access to it.",
             "a refusal of the key or of this model, which says nothing about the team tools."),
            (401, "Invalid API key", "a refusal of the key or of this model, which says nothing about the team tools."),
            (403, "Forbidden", "a refusal of the key or of this model, which says nothing about the team tools."))
        for status, words, ending in cases:
            endpoint = self.endpoint(["gpt-6.1-sol"], lambda body, status=status, words=words: (
                status, {"error": {"message": words, "type": "error"}}))
            with self.subTest(status=status):
                result = server.probe_team_tools(self.entry(endpoint), endpoint.models)
                self.assertIsNone(result["ok"])
                self.assertEqual(result["message"], "The team tools were not checked: the request with them for "
                                                    f"gpt-6.1-sol failed (HTTP {status}: {words}), {ending}")
                self.assertEqual(len(endpoint.team_requests()), 1)
        # No answer at all.
        closed = self.endpoint(["gpt-6.1-sol"], lambda body: (200, TEXT))
        entry = self.entry(closed)
        closed.close()
        result = server.probe_team_tools(entry, ["gpt-6.1-sol"], timeout=5)
        self.assertIsNone(result["ok"])
        self.assertEqual(result["message"], "The team tools were not checked: the request with them for gpt-6.1-sol "
                                            "failed. The connection was refused (ConnectionRefusedError). Test again "
                                            "later.")

    def test_another_refusal_is_the_team_tools_only_when_the_same_request_without_them_is_answered(self):
        refusal = {"error": {"message": "Unsupported value: 'max' is not supported with this model.",
                             "type": "invalid_request_error"}}
        for answer, ok, ending in (
                (with_tools_refused(400, refusal), False,
                 ", but answers the same request without them. A coordinator on this connection could not reach its "
                 "team."),
                # Refused without them too: the request's other settings, not its team tools.
                (lambda body: (400, refusal), None,
                 ", and so did the same request without them, so the refusal is not of the team tools."),
                # Answered without them, but its stream failed: not answered either.
                (with_tools_refused(422, refusal, [{"type": "response.failed", "response": {
                    "error": {"message": "upstream busy"}}}]), None,
                 ", and so did the same request without them, so the refusal is not of the team tools.")):
            endpoint = self.endpoint(["gpt-6.1-sol"], answer)
            with self.subTest(ok=ok, ending=ending):
                result = server.probe_team_tools(self.entry(endpoint), endpoint.models, effort="high")
                self.assertIs(result["ok"], ok)
                self.assertTrue(result["message"].endswith(ending), result["message"])
                with_tools, without = endpoint.team_requests()
                # The same request without the team tools: no additional_tools item, tool choice or parallel calls;
                # everything else as before.
                self.assertEqual([item["type"] for item in without["input"]], ["message"])
                self.assertEqual(with_tools["input"][1], without["input"][0])
                for name in ("tools", "tool_choice", "parallel_tool_calls"):
                    self.assertNotIn(name, without)
                self.assertEqual({key: value for key, value in with_tools.items()
                                  if key not in ("input", "tool_choice", "parallel_tool_calls")},
                                 {key: value for key, value in without.items() if key != "input"})
                self.assertEqual(without["reasoning"]["context"], "all_turns")
        # A model that does not run as Responses Lite: the request without its tools has none either.
        endpoint = self.endpoint(["lab-model"], with_tools_refused(400, refusal))
        self.assertFalse(server.probe_team_tools(self.entry(endpoint), endpoint.models)["ok"])
        with_tools, without = endpoint.team_requests()
        self.assertIn("tools", with_tools)
        self.assertNotIn("tools", without)


class RouteTests(unittest.TestCase):
    """POST /api-models/test of a connection (no model): its models, then the team-tool request, recorded."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        store = str(self.root / "state" / "api-models.json")
        for change in (mock.patch.dict(os.environ, {providers.STORE_ENV: store}),
                       mock.patch.object(server, "grant_windows_access")):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(providers._LAST_GOOD.pop, store, None)
        self.http = server.Server((server.HOST, 0), runs=self.root / "runs")
        self.addCleanup(self.http.server_close)

    def test_the_connection_s_test_records_whether_team_tools_reach_the_model(self):
        endpoint = Endpoint(["gpt-6.1-sol"], lambda body: (200, call("mcp__tcs__report_result")))
        self.addCleanup(endpoint.close)
        saved = self.http.save_api_model({"format": "openai", "baseUrl": endpoint.base, "apiKey": KEY,
                                          "label": "Proxy"})["connection"]
        result = self.http.probe_api_model({"id": saved["id"], "format": "openai", "baseUrl": endpoint.base})
        self.assertTrue(result["ok"])
        self.assertEqual((result["teamTools"]["ok"], result["teamTools"]["model"]), (False, "gpt-6.1-sol"))
        last = providers.api_connection(saved["id"], cached=False)["lastTest"]
        self.assertEqual(last["teamTools"]["message"], result["teamTools"]["message"])
        self.assertFalse(last["teamTools"]["ok"])

    def test_a_test_whose_team_tool_request_said_nothing_about_them_records_them_not_checked(self):
        endpoint = Endpoint(["gpt-6.1-sol"], lambda body: (429, {"error": {
            "message": "All credentials for model gpt-6.1-sol are cooling down", "type": "rate_limit_error"}}))
        self.addCleanup(endpoint.close)
        saved = self.http.save_api_model({"format": "openai", "baseUrl": endpoint.base, "apiKey": KEY,
                                          "label": "Proxy"})["connection"]
        result = self.http.probe_api_model({"id": saved["id"], "format": "openai", "baseUrl": endpoint.base})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["teamTools"]["ok"])
        last = providers.api_connection(saved["id"], cached=False)["lastTest"]
        self.assertIsNone(last["teamTools"]["ok"])
        self.assertEqual(last["teamTools"]["message"], result["teamTools"]["message"])
        self.assertTrue(last["teamTools"]["message"].startswith("The team tools were not checked: "))

    def test_an_anthropic_connection_s_test_sends_no_team_tool_request(self):
        endpoint = Endpoint(["claude-gw"], lambda body: (500, {}))
        self.addCleanup(endpoint.close)
        entry = {"format": "anthropic", "baseUrl": endpoint.base.removesuffix("/v1"), "requestSource": "",
                 "apiKey": KEY}
        with mock.patch.object(providers, "validated_api_connection", return_value=entry), \
                mock.patch.object(server, "discover_api_models", return_value={
                    "ok": True, "status": 200, "message": "Found 1 model.", "models": [{"id": "claude-gw"}],
                    "latencyMs": 3, "testedAt": "2026-11-14T00:00:00+00:00"}):
            result = self.http.probe_api_model({"format": "anthropic", "baseUrl": endpoint.base, "apiKey": KEY})
        self.assertNotIn("teamTools", result)
        self.assertEqual(endpoint.requests, [])


if __name__ == "__main__":
    unittest.main()
