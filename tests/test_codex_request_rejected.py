"""Codex's report of an endpoint's HTTP 400 invalid_request_error on an API connection: a request the endpoint refuses.

Codex 0.160 reports an OpenAI-format endpoint's HTTP 400 as the endpoint's error body alone, with no "400" anywhere
(tests/codex_refusals.py has the lines it printed against a fake endpoint, through `codex exec --json` and its
app-server). A failure whose text quotes an error of type invalid_request_error from an API connection is such a
request (workflow_runner.settings_failure(), RequestRejected), as Claude Code's "API Error: 400" is
(tests/test_claude_request_rejected.py), the owner's decision of 13 Nov 2026: a structured call ends after its first
attempt, the author pauses with a message that says to change the connection's settings (a job waiting for it
continues once they change), a paused author's reply fails as a settings problem, and a team member's assignment comes
back to the coordinator as its API connection's refusal (team.REFUSED_REPORT, the owner's wording of 14 Nov 2026; so
does an endpoint that says the model does not exist, which the author and a structured call report as "Model
unavailable"). The endpoint's words in these messages are its error's message, also when it writes its body over
several lines, as api.openai.com does.

Codex 0.160 reports every other status in words of its own, never as the body alone, so a failure that is only error
bodies is an endpoint's 400 whatever the body: other servers' bodies (vLLM's {"object": "error", ...}, an "error"
that is a string, FastAPI's {"detail": ...}) are such requests too, and the words of a passing condition (a 5xx
number, 429, capacity, temporary, ...) keep it retried only outside the bodies: "input[512]" in the endpoint's message
is no server error.

Everything else stays retried every minute: an endpoint's 400 for a conversation too long for the model (in any of
those bodies), a passing condition named outside the bodies, a body quoted under another HTTP status (a key refused
with 401), a 400 whose body is plain text (which cannot be told from Codex's own words), and the same body from a
built-in model (the ChatGPT route). The stand-in programs replay the captured lines; no test starts a model.
"""

import functools
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import claude_cli, providers, team
import workflow_runner as runtime

import codex_refusals as captured
import test_author_conversation as conversation_test
import test_decisions_jobs as jobs_test
import test_goal_runtime as goal_test
import test_model_retry as retry_test

KEY = "sk-gateway-0123456789abcdef"
ENDPOINT_MESSAGE = captured.ENDPOINT_MESSAGE
# What the job says for the OpenAI-format connection "Gateway": a structured call ends with it, the author pauses with
# it; a team member's activity-log line says only what failed, ending with the endpoint's words (REJECTED_LINE).
REJECTED_LINE = (f"{runtime.REQUEST_REJECTED_LABEL}: Gateway refuses the requests this job sends as invalid "
                 f"({ENDPOINT_MESSAGE}).")
REJECTED = (f"{REJECTED_LINE} A retry cannot fix this: change its API connection's settings under Models & API (or "
            "the endpoint); a job waiting for it continues once its settings change.")
# The endpoint's words for a model it does not have (EXEC_400_NO_MODEL, EXEC_404_NO_MODEL), and for a key it refuses.
NO_MODEL = "The requested model 'lab-model' does not exist."
NOT_FOUND = ("unexpected status 404 Not Found: The model `lab-model` does not exist or you do not have access to it., "
             "url: http://127.0.0.1:65370/v1/responses")
BAD_KEY = ("unexpected status 401 Unauthorized: Incorrect API key provided: sk-fake-********cdef., url: "
           "http://127.0.0.1:65370/v1/responses")
VALID = retry_test.VALID


def save_gateway():
    return providers.save_api_model({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                     "headerName": "Authorization", "model": "lab-model", "apiKey": KEY,
                                     "label": "Gateway"})["id"]


def author_text(lines=captured.APP_SERVER_400):
    """The author's failure text of the app-server's error notification (goal_session(), author_conversation())."""

    return runtime._provider_message(captured.app_server_error(lines))


def with_message(lines, message):
    """Captured lines with the endpoint's message replaced: the same body from an endpoint that says ``message``."""

    return tuple(line.replace(ENDPOINT_MESSAGE, message) for line in lines)


class CapturedTextTests(jobs_test.Isolated):
    """settings_failure() and member_problem() on the runner's texts of the captured lines, for each path."""

    def setUp(self):
        super().setUp()
        self.model = save_gateway()

    def texts(self, lines=captured.EXEC_400):
        """The failure texts the runner makes of an exec's lines: a member's, a structured call's (two stages)."""

        return {"member": captured.exec_text(lines), "review": captured.exec_text(lines, "Codex review failed: "),
                "critic": captured.exec_text(lines, "Codex critic failed: ")}

    def test_the_captured_400_is_a_request_the_endpoint_refuses_on_every_path(self):
        turn = json.loads(captured.APP_SERVER_400[1])["params"]["turn"]["error"]
        texts = {**self.texts(), "author": author_text(),
                 # The author's (and a paused author's reply's) failed turn, when it ends before the notification.
                 "author turn": f"Author turn failed: {runtime._provider_message(turn)}",
                 "reply turn": f"Author reply turn failed: {runtime._provider_message(turn)}"}
        # The text names no HTTP status at all: only the error's type says what it is.
        self.assertEqual(texts["author"], json.loads(captured.APP_SERVER_400[0])["params"]["error"]["message"])
        self.assertNotIn("400", "".join(texts.values()))
        for path, text in texts.items():
            with self.subTest(path=path):
                problem = runtime.settings_failure(runtime.Error(text), self.model)
                self.assertIsInstance(problem, runtime.RequestRejected)
                self.assertEqual(str(problem), REJECTED)
                self.assertEqual((problem.reason, problem.summary), (ENDPOINT_MESSAGE, REJECTED_LINE))
                self.assertFalse(runtime.retryable_failure(problem))
                # A team member's assignment comes back as its API connection's refusal, with the endpoint's words.
                self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                 team.Refusal(REJECTED_LINE, ENDPOINT_MESSAGE))

    def test_a_body_written_over_several_lines_is_refused_in_the_endpoint_words(self):
        for path, text in self.texts(captured.EXEC_400_INDENTED).items():
            with self.subTest(path=path):
                self.assertIn('\n    "error": {\n        "message": ', text)
                self.assertEqual(str(runtime.settings_failure(runtime.Error(text), self.model)), REJECTED)
                self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                 team.Refusal(REJECTED_LINE, ENDPOINT_MESSAGE))

    def test_the_same_body_from_a_built_in_model_stays_retried(self):
        for path, text in {**self.texts(), "author": author_text()}.items():
            with self.subTest(path=path):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), "gpt-6.1-sol"))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), "gpt-6.1-sol"))
                self.assertTrue(runtime.retryable_failure(runtime.Error(text)))

    def test_a_conversation_too_long_stays_retried(self):
        texts = [*self.texts(captured.EXEC_400_TOO_LONG).values(), *self.texts(captured.EXEC_400_VLLM_TOO_LONG).values()]
        # A conversation too large for the model in the code of a proxy such as CLIProxyAPI (whose message need not say
        # so).
        texts.append(captured.exec_text(tuple(line.replace("unsupported_parameter", "context_too_large") for line in
                                              with_message(captured.EXEC_400, "Request has too many tokens."))))
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), self.model))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), self.model))
        # The conversation too long is one, also in vLLM's body: the author continues in a new conversation
        # (goal_session()).
        for lines in (captured.EXEC_400_TOO_LONG, captured.EXEC_400_VLLM_TOO_LONG):
            self.assertTrue(runtime._context_window_failure(captured.exec_text(lines)))

    def test_numbers_and_words_in_the_endpoint_s_message_never_keep_a_refused_request_retried(self):
        # The endpoint's message names an item by its number (input[512], input[429]) or says words that elsewhere
        # name a passing condition: it is still the endpoint's 400, as Codex reports no other status as a body alone.
        cases = [(captured.EXEC_400_INPUT_512, captured.INPUT_512_MESSAGE),
                 (captured.EXEC_400_INPUT_429, captured.INPUT_429_MESSAGE)]
        for message in ("The server is overloaded, try again later.", "Rate limit reached for requests.",
                        "The upstream answered 503; try again.", "No capacity for 'service_tier': 'flex'.",
                        "Temporary files cannot be attached to this model.", "High demand models need 'priority'."):
            cases.append((with_message(captured.EXEC_400, message), message))
        for lines, message in cases:
            line = f"{runtime.REQUEST_REJECTED_LABEL}: Gateway refuses the requests this job sends as invalid ({message})."
            for path, text in self.texts(lines).items():
                with self.subTest(message=message, path=path):
                    self.assertRegex(text, r"(?i)\b(?:5\d\d|429)\b|capacity|overload|rate limit|temporar|high demand")
                    problem = runtime.settings_failure(runtime.Error(text), self.model)
                    self.assertIsInstance(problem, runtime.RequestRejected)
                    self.assertEqual((problem.reason, problem.summary), (message, line))
                    self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                     team.Refusal(line, message))
        # The author's app-server reports it the same way.
        turn = json.loads(captured.APP_SERVER_400_INPUT_512[1])["params"]["turn"]["error"]
        for text in (author_text(captured.APP_SERVER_400_INPUT_512),
                     f"Author turn failed: {runtime._provider_message(turn)}"):
            with self.subTest(text=text):
                self.assertEqual(runtime.settings_failure(runtime.Error(text), self.model).reason,
                                 captured.INPUT_512_MESSAGE)

    def test_a_failure_that_names_a_status_outside_its_bodies_is_what_that_status_says(self):
        # Codex names every status but a 400 in its own words around the body: then the body's words count as before
        # (_bare_error_body() finds no bare 400), so "Temporarily" in a 404's body keeps it a passing failure.
        quoted = ('Codex member failed: unexpected status 404 Not Found: {"error": {"message": "Temporarily '
                  'unavailable."}}, url: https://gateway.example.com/v1/responses')
        self.assertIsNone(runtime._bare_error_body(quoted))
        self.assertEqual(runtime._bare_error_body(captured.exec_text(captured.EXEC_400)), "Codex member failed: \n")

    def test_a_body_inside_a_body_is_part_of_it(self):
        # A gateway that nests its upstream's error in its own: all of it is the endpoint's words, so the 503 after
        # the inner body (in the outer one) is no passing condition.
        body = ('{"error": {"upstream": {"error": {"message": "Unsupported parameter."}}, "message": "Upstream '
                'answered 503 for an unsupported parameter.", "type": "invalid_request_error"}}')
        problem = runtime.settings_failure(runtime.Error(f"Codex member failed: {body}\n{body}"), self.model)
        self.assertIsInstance(problem, runtime.RequestRejected)
        self.assertEqual(problem.reason, "Upstream answered 503 for an unsupported parameter.")

    def test_a_passing_condition_outside_the_bodies_keeps_the_failure_retried(self):
        body = captured.exec_text(captured.EXEC_400)
        for text in (
                # Codex's own words for a 500 ahead of the body, and its warning about a temporary folder in stderr.
                "Codex member failed: We're currently experiencing high demand, which may cause temporary errors.\n"
                + body.removeprefix("Codex member failed: "),
                body + "\nWARNING: Refusing to create helper binaries under temporary dir \"/tmp/x\"",
                # A body quoted under a status, which then says what it is: 503 retried as before.
                'Codex member failed: unexpected status 503 Service Unavailable: {"error": {"type": '
                '"invalid_request_error", "message": "Upstream down."}}, url: https://gateway.example.com/v1/responses'):
            with self.subTest(text=text):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), self.model))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), self.model))
                self.assertTrue(runtime.retryable_failure(runtime.Error(text)))

    def test_other_servers_400_bodies_are_requests_the_endpoint_refuses(self):
        # vLLM's BadRequestError (with no invalid_request_error anywhere), with and without the "error" wrapper; an
        # "error" that is a string (Ollama's); FastAPI's {"detail": ...}. The reason is the body's own words.
        cases = ((captured.EXEC_400_VLLM, captured.VLLM_MESSAGE), (captured.EXEC_400_VLLM_WRAPPED, captured.VLLM_MESSAGE),
                 (captured.EXEC_400_ERROR_TEXT, "lab-model does not support tools"),
                 (captured.EXEC_400_DETAIL, "Bad Request"))
        for lines, reason in cases:
            line = f"{runtime.REQUEST_REJECTED_LABEL}: Gateway refuses the requests this job sends as invalid ({reason})."
            for path, text in self.texts(lines).items():
                with self.subTest(reason=reason, path=path):
                    self.assertNotIn("invalid_request", text)
                    problem = runtime.settings_failure(runtime.Error(text), self.model)
                    self.assertIsInstance(problem, runtime.RequestRejected)
                    self.assertEqual(str(problem), f"{line} A retry cannot fix this: change its API connection's "
                                                   "settings under Models & API (or the endpoint); a job waiting for "
                                                   "it continues once its settings change.")
                    self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                     team.Refusal(line, reason))
                    # A built-in model's same failure stays retried.
                    self.assertIsNone(runtime.settings_failure(runtime.Error(text), "gpt-6.1-sol"))
        turn = json.loads(captured.APP_SERVER_400_VLLM[1])["params"]["turn"]["error"]
        for text in (author_text(captured.APP_SERVER_400_VLLM), f"Author turn failed: {runtime._provider_message(turn)}",
                     f"Author reply turn failed: {runtime._provider_message(turn)}"):
            with self.subTest(text=text):
                self.assertEqual(runtime.settings_failure(runtime.Error(text), self.model).reason, captured.VLLM_MESSAGE)

    def test_a_400_whose_body_is_plain_text_stays_retried(self):
        # Codex 0.160 reports it as those words alone (EXEC_400_PLAIN), which nothing tells from its own words.
        for path, text in self.texts(captured.EXEC_400_PLAIN).items():
            with self.subTest(path=path):
                self.assertEqual(text.split(": ", 1)[1], "Bad Request\nBad Request")
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), self.model))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), self.model))

    def test_a_content_filter_s_refusal_as_its_body_alone_stays_retried(self):
        # Owner's decision (14 Nov 2026): a content filter's refusal is no setting of the connection and is retried
        # every minute. Codex 0.160 reports it as the endpoint's body alone, typed invalid_request_error and naming no
        # status (EXEC_400_FILTERED, APP_SERVER_400_FILTERED), as it reports every 400: the exclusion of the filter's
        # refusals covers the rule for such bodies too.
        turn = json.loads(captured.APP_SERVER_400_FILTERED[1])["params"]["turn"]["error"]
        texts = {**self.texts(captured.EXEC_400_FILTERED), "author": author_text(captured.APP_SERVER_400_FILTERED),
                 "author turn": f"Author turn failed: {runtime._provider_message(turn)}",
                 "reply turn": f"Author reply turn failed: {runtime._provider_message(turn)}"}
        for path, text in texts.items():
            with self.subTest(path=path):
                self.assertNotIn("400", text)
                self.assertIn('"type": "invalid_request_error"', text)
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), self.model))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), self.model))
                self.assertTrue(runtime.retryable_failure(runtime.Error(text)))
        # Without the filter's words the same body is a request the endpoint refuses.
        unfiltered = texts["member"].replace("content management policy", "usage policy").replace(
            "content_filter", "policy_violation")
        self.assertEqual(runtime.member_problem(runtime.Error(unfiltered), self.model).reason,
                         "The response was filtered due to the prompt triggering Azure OpenAI's usage policy. Please "
                         "modify your prompt and retry.")

    def test_a_body_quoted_under_another_status_is_what_that_status_says(self):
        # Codex quotes a body after its own words for another status when the body has no message of its own
        # (its display_body()): a key refused with 401 stays a failed authentication, retried for the author (the job
        # page says that the key was rejected) and a refusal for a team member, as the captured 401 is.
        quoted = ('unexpected status 401 Unauthorized: {"error": {"type": "invalid_request_error", "code": '
                  '"invalid_api_key"}}, url: https://gateway.example.com/v1/responses')
        for reason in (quoted, BAD_KEY):
            text = captured.exec_text(captured.EXEC_401).replace(BAD_KEY, reason)
            with self.subTest(reason=reason):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), self.model))
                self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                 team.Refusal(f"authentication failed: {reason}", reason))

    def test_a_model_the_endpoint_does_not_have_is_unavailable_and_a_members_refusal(self):
        for lines, reason in ((captured.EXEC_400_NO_MODEL, NO_MODEL), (captured.EXEC_404_NO_MODEL, NOT_FOUND)):
            line = f"{runtime.MODEL_UNAVAILABLE_LABEL}: Gateway cannot be used ({reason})."
            for path, text in self.texts(lines).items():
                with self.subTest(reason=reason, path=path):
                    # The author and a structured call keep their message, with what to do.
                    problem = runtime.settings_failure(runtime.Error(text), self.model)
                    self.assertIsInstance(problem, runtime.ModelUnavailable)
                    self.assertEqual(str(problem), f"{line} A retry cannot fix this: check its API connection under "
                                                   "Models & API (Load models lists the models its key can use).")
                    self.assertEqual((problem.reason, problem.summary), (reason, line))
                    # A team member on the API connection: the refusal's report, its line ending with the words.
                    self.assertEqual(runtime.member_problem(runtime.Error(text), self.model),
                                     team.Refusal(line, reason))
                    # A built-in model's unavailable model reports as before, with what to do.
                    self.assertEqual(runtime.member_problem(runtime.Error(text), "gpt-6.1-sol"),
                                     f"{runtime.MODEL_UNAVAILABLE_LABEL}: GPT 6.1 Sol cannot be used ({reason}). A "
                                     "retry cannot fix this: check that the model exists and that your login may use "
                                     "it.")


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class StructuredCallTests(retry_test.RetryCase):
    """A structured call whose stand-in `codex exec --json` prints the captured lines (tests/test_model_retry.py's)."""

    def setUp(self):
        super().setUp()
        self.state = self.root / "codex-state"
        self.state.mkdir()
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n{retry_test.CODEX_FAKE}")
        fake.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(self.state),
                                                    "CODEX_HOME": str(self.root / "codex-home"),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live",
                                                    providers.STORE_ENV: str(self.root / "state/api-models.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(providers._LAST_GOOD.pop, str(self.root / "state/api-models.json"), None)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        for name in ("codex", "api_codex_executable"):
            located = mock.patch.object(runtime, name, side_effect=lambda *args, fake=fake: str(fake))
            located.start()
            self.addCleanup(located.stop)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.model = save_gateway()

    def script(self, *behaviours):
        (self.state / "script.json").write_text(json.dumps(list(behaviours)))

    def execs(self):
        path = self.state / "log.jsonl"
        return [entry for entry in map(json.loads, path.read_text().splitlines()) if "argv" in entry]

    def test_an_api_connection_s_400_ends_the_call_after_its_first_attempt(self):
        self.script({"lines": captured.EXEC_400}, {"answer": VALID})
        with self.assertRaises(runtime.RequestRejected) as raised:
            runtime.structured("Review S.", retry_test.SCHEMA, "review", model=self.model, effort="high")
        self.assertEqual(str(raised.exception), REJECTED)
        # The runner's text of the lines, as the other tests of this module read it.
        self.assertEqual(str(raised.exception.__cause__),
                         captured.exec_text(captured.EXEC_400, "Codex review failed: "))
        self.assertEqual((len(self.execs()), self.waits()), (1, 0))
        self.assertFalse(self.statuses())

    def test_a_built_in_models_call_is_retried(self):
        self.script({"lines": captured.EXEC_400}, {"answer": VALID})
        result, _ = runtime.structured("Review S.", retry_test.SCHEMA, "review", model="gpt-6.1-sol", effort="high")
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        self.assertEqual((len(self.execs()), self.waits()), (2, 60))

    def refused_once(self, lines, reason):
        self.script({"lines": lines}, {"answer": VALID})
        with self.assertRaises(runtime.RequestRejected) as raised:
            runtime.structured("Review S.", retry_test.SCHEMA, "review", model=self.model, effort="high")
        self.assertEqual(raised.exception.reason, reason)
        self.assertEqual((len(self.execs()), self.waits()), (1, 0))

    def test_another_server_s_400_ends_the_call_after_its_first_attempt(self):
        self.refused_once(captured.EXEC_400_VLLM, captured.VLLM_MESSAGE)

    def test_a_400_whose_message_names_input_512_ends_the_call_after_its_first_attempt(self):
        self.refused_once(captured.EXEC_400_INPUT_512, captured.INPUT_512_MESSAGE)

    def test_a_content_filter_s_refusal_is_retried(self):
        self.script({"lines": captured.EXEC_400_FILTERED}, {"answer": VALID})
        result, _ = runtime.structured("Review S.", retry_test.SCHEMA, "review", model=self.model, effort="high")
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        self.assertEqual((len(self.execs()), self.waits()), (2, 60))

    def test_a_conversation_too_long_for_the_model_is_retried(self):
        self.script({"lines": captured.EXEC_400_TOO_LONG}, {"answer": VALID})
        result, _ = runtime.structured("Review S.", retry_test.SCHEMA, "review", model=self.model, effort="high")
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        self.assertEqual((len(self.execs()), self.waits()), (2, 60))


class CapturedAppServer(retry_test.AppServer):
    """tests/test_model_retry.py's scripted app-server; a script {"captured": lines} answers turn/start with the lines
    the real app-server printed (tests/codex_refusals.py), in this thread and turn."""

    def call(self, method, params):
        if method != "turn/start" or "captured" not in self.test.scripts[0]:
            return super().call(method, params)
        self.calls.append((method, params))
        script, thread = self.test.scripts.pop(0), params["threadId"]
        self.test.turn_count += 1
        turn = f"turn-{self.test.turn_count}"
        self.test.threads[thread].append({"id": turn, "input": params["input"][0]["text"], "items": []})
        self.queue("turn/started", thread, turn={"id": turn})
        for line in script["captured"]:
            message = json.loads(line)
            message["params"]["threadId"] = thread
            if "turnId" in message["params"]:
                message["params"]["turnId"] = turn
            if isinstance(message["params"].get("turn"), dict):
                message["params"]["turn"]["id"] = turn
            self.messages.append(message)
        return {"turn": {"id": turn}}


class AuthorTests(retry_test.RetryCase):
    """The Codex author's goal session, whose scripted app-server reports the captured 400."""

    PROMPTS = retry_test.CodexAuthorTests.PROMPTS
    launcher = retry_test.CodexAuthorTests.launcher
    run_goal = retry_test.CodexAuthorTests.run_goal

    def setUp(self):
        super().setUp()
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.servers, self.threads, self.failures = [], {}, {}
        self.scripts = [{"captured": captured.APP_SERVER_400}, {"answer": "Full proof"}]
        self.turn_count, self.replacement, self.safety_reads = 0, None, []
        home = self.root / "api-home"
        home.mkdir()
        self.codex_home = str(home)  # The private home the app-server reports using (an API model's).
        store = str(self.root / "state/api-models.json")
        for target, name, value in (
                (runtime, "RPC", functools.partial(CapturedAppServer, self)), (runtime, "codex", lambda: "codex-test"),
                (runtime, "api_codex_executable", lambda environment: "codex-test"),
                (runtime, "api_codex_home", lambda directory=None: home),
                (runtime.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
                    poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0, kill=lambda: None))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live", providers.STORE_ENV: store,
                                                    "CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(providers._LAST_GOOD.pop, store, None)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def test_an_api_connection_s_400_pauses_the_author_with_the_message_and_no_retry(self):
        model = save_gateway()
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal(launch=self.launcher(model=model))
        self.assertEqual(raised.exception.reason, REJECTED)
        self.assertIsNone(raised.exception.retry)
        paused, = self.statuses("Author paused")
        self.assertEqual(paused["text"], REJECTED)
        self.assertEqual((len(self.servers), self.waits()), (1, 0))

    def test_another_server_s_400_and_a_number_in_its_message_pause_the_author_without_a_retry(self):
        for lines, reason in ((captured.APP_SERVER_400_VLLM, captured.VLLM_MESSAGE),
                              (captured.APP_SERVER_400_INPUT_512, captured.INPUT_512_MESSAGE)):
            with self.subTest(reason=reason):
                self.servers.clear()
                self.scripts[:] = [{"captured": lines}, {"answer": "Full proof"}]
                with self.assertRaises(runtime.WorkflowPaused) as raised:
                    self.run_goal(launch=self.launcher(model=save_gateway()))
                self.assertIn(f"invalid ({reason}). A retry cannot fix this", raised.exception.reason)
                self.assertIsNone(raised.exception.retry)
                self.assertEqual(len(self.servers), 1)
        self.assertEqual(self.waits(), 0)

    def test_a_content_filter_s_refusal_is_retried_in_the_same_conversation(self):
        self.scripts[0] = {"captured": captured.APP_SERVER_400_FILTERED}
        self.assertEqual(self.run_goal(launch=self.launcher(model=save_gateway())),
                         {"outcome": "done", "output": "Full proof"})
        self.assertEqual((len(self.servers), self.waits()), (2, 60))
        self.assertFalse(self.statuses("Author paused") or self.statuses("Conversation replaced"))

    def test_a_built_in_models_author_is_retried(self):
        self.codex_home = None
        self.assertEqual(self.run_goal(), {"outcome": "done", "output": "Full proof"})
        self.assertEqual((len(self.servers), self.waits()), (2, 60))
        self.assertFalse(self.statuses("Author paused"))


class ReplyTests(unittest.TestCase):
    """A paused Codex author's reply turn, whose app-server reports the captured 400."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.clock = retry_test.FakeClock()
        retry_test.patch_clock(self, self.clock)
        self.events = []
        store = str(self.root / "state/api-models.json")
        for patcher in (mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append(
                            {"kind": kind, "stage": stage, **fields})),
                        mock.patch.object(runtime, "require_model_credentials"),
                        mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()),
                        mock.patch.object(runtime, "goal_session", side_effect=AssertionError("goal session")),
                        mock.patch.object(claude_cli, "goal_session", side_effect=AssertionError("goal session")),
                        mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live", providers.STORE_ENV: store})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(providers._LAST_GOOD.pop, store, None)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def test_the_reply_fails_as_a_settings_problem_once(self):
        # The turn's failure, from the app-server's lines (in the double's thread and turn).
        notification = json.loads(captured.APP_SERVER_400[0])
        notification["params"].update(threadId="thread-1", turnId="turn-1")
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            runtime.author_conversation(
                conversation_test.Codex([[notification]]), "Which step is the main obstacle?",
                settings=goal_test.SETTINGS, options={"goal_cwd": str(self.root), "goal_thread_id": "thread-1"},
                features=[], conversation_id=conversation_test.CONVERSATION_ID, label="Message")
        self.assertEqual(str(raised.exception), author_text())
        # The reply of a job whose author runs on the API connection: no retry, the message once.
        model = save_gateway()
        turns = conversation_test.FakeTurns([raised.exception])
        with mock.patch.object(runtime, "author_conversation", side_effect=turns):
            code = runtime.run_author_conversation(conversation_test.WORKFLOW, {
                "author_model": model, "effort": "high", "speed": "standard", "summary": "concise",
                "goal_cwd": str(self.root), "goal_thread_id": "thread-1"},
                {"schemaVersion": 1, "id": conversation_test.CONVERSATION_ID, "kind": "message",
                 "text": "Which step is the main obstacle?", "notice": ""})
        self.assertEqual(code, 0)
        self.assertEqual(len(turns.calls), 1)
        failed, = [event for event in self.events if event.get("label") == "Author reply failed"]
        self.assertEqual((failed["failure"], failed["final"], failed["text"]), ("configuration", True, REJECTED))
        self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
