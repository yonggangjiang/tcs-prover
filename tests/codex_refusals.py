"""What the real Codex 0.160 printed when an OpenAI-format endpoint refused its request (captured 14 Nov 2026).

The endpoint was a fake one on 127.0.0.1 (port 65370 in the lines that name it) that answered every request to
/v1/responses with one HTTP status and error body. Codex ran with a private HOME and CODEX_HOME, a fake key, the
provider settings of an API connection (workflow_runner.provider_arguments()), stdin closed, and sandbox-exec allowing
connections to localhost only. Each tuple holds lines exactly as Codex printed them: for `codex exec --json` (a team
member's or a structured call's run) those after its thread.started, item.completed (a notice that it has no metadata
for the model) and turn.started lines, before it exited with code 1; for its app-server (the author's), the error
notification and the turn/completed that followed turn/start.

- HTTP 400 invalid_request_error, its body on one line as Python's json.dumps writes it: Codex reports the body alone,
  with no status (its InvalidRequest error), through `codex exec --json` (EXEC_400) and the app-server
  (APP_SERVER_400: codexErrorInfo "other", willRetry false), after one request; Codex never retried it itself.
- The same body written over several lines, as api.openai.com writes its bodies (EXEC_400_INDENTED).
- HTTP 400 for a conversation too long for the model, code context_length_exceeded (EXEC_400_TOO_LONG).
- A model the endpoint does not have, code model_not_found: HTTP 400 as the Responses API answers it
  (EXEC_400_NO_MODEL, the body alone again) and HTTP 404 as Chat Completions does (EXEC_404_NO_MODEL).
- HTTP 401 for a key the endpoint refuses (EXEC_401).
For the 404 and the 401 Codex used its own words, "unexpected status <status>: <the error's message>, url: <the
URL>", after five retries of its own ("Reconnecting... <n>/5 (...)", which the runner skips).
"""

import json

# The endpoint's own message in EXEC_400, APP_SERVER_400 and EXEC_400_INDENTED.
ENDPOINT_MESSAGE = "Unsupported parameter: 'reasoning.summary' is not supported with this model."

EXEC_400 = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"Unsupported parameter: 'reasoning.summary' is '''
    r'''not supported with this model.\", \"type\": \"invalid_request_error\", \"param\": \"reasoning.summary\",'''
    r''' \"code\": \"unsupported_parameter\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"Unsupported '''
    r'''parameter: 'reasoning.summary' is not supported with this model.\", \"type\": \"invalid_request_error\",'''
    r''' \"param\": \"reasoning.summary\", \"code\": \"unsupported_parameter\"}}"}}''',
)
APP_SERVER_400 = (
    r'''{"method":"error","params":{"error":{"message":"{\"error\": {\"message\": \"Unsupported '''
    r'''parameter: 'reasoning.summary' is not supported with this model.\", \"type\": \"invalid_request_error\",'''
    r''' \"param\": \"reasoning.summary\", \"code\": \"unsupported_parameter\"}}","codexErrorInfo":"other",'''
    r'''"additionalDetails":null,"misalignment":null},"willRetry":false,'''
    r'''"threadId":"01a1d97a-1350-7873-8168-798291301c68","turnId":"01a1d97a-135b-7e10-babf-a7dea79bbe4c"},'''
    r'''"emittedAtMs":1794650018670}''',
    r'''{"method":"turn/completed","params":{"threadId":"01a1d97a-1350-7873-8168-798291301c68",'''
    r'''"turn":{"id":"01a1d97a-135b-7e10-babf-a7dea79bbe4c","items":[],"itemsView":"notLoaded",'''
    r'''"status":"failed","error":{"message":"{\"error\": {\"message\": \"Unsupported '''
    r'''parameter: 'reasoning.summary' is not supported with this model.\", \"type\": \"invalid_request_error\",'''
    r''' \"param\": \"reasoning.summary\", \"code\": \"unsupported_parameter\"}}","codexErrorInfo":"other",'''
    r'''"additionalDetails":null,"misalignment":null},"startedAt":1794650018,"completedAt":1794650018,'''
    r'''"durationMs":17}},"emittedAtMs":1794650018671}''',
)
EXEC_400_INDENTED = (
    r'''{"type":"error","message":"{\n    \"error\": {\n        \"message\": \"Unsupported '''
    r'''parameter: 'reasoning.summary' is not supported with this model.\",\n        \"type\": '''
    r'''\"invalid_request_error\",\n        \"param\": \"reasoning.summary\",\n        \"code\": '''
    r'''\"unsupported_parameter\"\n    }\n}\n"}''',
    r'''{"type":"turn.failed","error":{"message":"{\n    \"error\": {\n        \"message\": \"Unsupported '''
    r'''parameter: 'reasoning.summary' is not supported with this model.\",\n        \"type\": '''
    r'''\"invalid_request_error\",\n        \"param\": \"reasoning.summary\",\n        \"code\": '''
    r'''\"unsupported_parameter\"\n    }\n}\n"}}''',
)
EXEC_400_TOO_LONG = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"Your input exceeds the context window of this '''
    r'''model. Please adjust your input and try again.\", \"type\": \"invalid_request_error\", \"param\": '''
    r'''\"input\", \"code\": \"context_length_exceeded\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"Your input exceeds the context '''
    r'''window of this model. Please adjust your input and try again.\", \"type\": \"invalid_request_error\", '''
    r'''\"param\": \"input\", \"code\": \"context_length_exceeded\"}}"}}''',
)
EXEC_400_NO_MODEL = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"The requested model 'lab-model' does not '''
    r'''exist.\", \"type\": \"invalid_request_error\", \"param\": \"model\", \"code\": \"model_not_found\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"The requested model 'lab-model' '''
    r'''does not exist.\", \"type\": \"invalid_request_error\", \"param\": \"model\", \"code\": '''
    r'''\"model_not_found\"}}"}}''',
)
EXEC_404_NO_MODEL = (
    r'''{"type":"error","message":"Reconnecting... 1/5 (unexpected status 404 Not Found: The model `lab-model` '''
    r'''does not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 2/5 (unexpected status 404 Not Found: The model `lab-model` '''
    r'''does not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 3/5 (unexpected status 404 Not Found: The model `lab-model` '''
    r'''does not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 4/5 (unexpected status 404 Not Found: The model `lab-model` '''
    r'''does not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 5/5 (unexpected status 404 Not Found: The model `lab-model` '''
    r'''does not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"unexpected status 404 Not Found: The model `lab-model` does not exist or you '''
    r'''do not have access to it., url: http://127.0.0.1:65370/v1/responses"}''',
    r'''{"type":"turn.failed","error":{"message":"unexpected status 404 Not Found: The model `lab-model` does '''
    r'''not exist or you do not have access to it., url: http://127.0.0.1:65370/v1/responses"}}''',
)
EXEC_401 = (
    r'''{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 3/5 (unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 4/5 (unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"Reconnecting... 5/5 (unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses)"}''',
    r'''{"type":"error","message":"unexpected status 401 Unauthorized: Incorrect API key provided: '''
    r'''sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses"}''',
    r'''{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key '''
    r'''provided: sk-fake-********cdef., url: http://127.0.0.1:65370/v1/responses"}}''',
)


def messages(lines):
    """The provider messages the runner keeps of `codex exec --json` lines (_codex_member_attempt() and
    _run_structured_process() alike): each error event's message, but Codex's own retries, and the failed turn's."""

    kept = []
    for event in map(json.loads, lines):
        if event["type"] == "error" and not event["message"].startswith("Reconnecting..."):
            kept.append(event["message"])
        elif event["type"] == "turn.failed" and event["error"]["message"].strip():
            kept.append(event["error"]["message"].strip())
    return kept


def exec_text(lines, prefix="Codex member failed: "):
    """The failure text the runner makes of `codex exec --json` lines ("Codex <stage> failed: ", then the messages)."""

    return prefix + "\n".join(messages(lines)).strip()


def app_server_error(lines=None):
    """The error that the app-server's error notification carries (the first line of APP_SERVER_400)."""

    return json.loads((lines or APP_SERVER_400)[0])["params"]["error"]
