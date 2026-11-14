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

Captured the same way on 14 Nov 2026 (later the same day, ~/tcs-work/scratch/coord-ready/capture), each after one
request, the body alone again, with ports and IDs as printed:
- HTTP 400 for a content filter's refusal typed invalid_request_error, with no status in its body (Azure OpenAI's
  message and code), through `codex exec --json` (EXEC_400_FILTERED) and the app-server (APP_SERVER_400_FILTERED).
- HTTP 400 invalid_request_error bodies whose messages name an item by its number, as api.openai.com words them:
  "input[512]" (EXEC_400_INPUT_512, APP_SERVER_400_INPUT_512) and "input[429]" (EXEC_400_INPUT_429).
- Other servers' HTTP 400 bodies: vLLM's {"object": "error", ..., "type": "BadRequestError", "code": 400}
  (EXEC_400_VLLM, APP_SERVER_400_VLLM), the same in an "error" wrapper as newer vLLM writes it
  (EXEC_400_VLLM_WRAPPED), vLLM's conversation too long for the model (EXEC_400_VLLM_TOO_LONG), an "error" that is a
  string, as Ollama writes it (EXEC_400_ERROR_TEXT), FastAPI's {"detail": ...} (EXEC_400_DETAIL), and a text/plain
  "Bad Request" (EXEC_400_PLAIN, which Codex reports as those words alone).
With TCS Prover's RUST_LOG=error (workflow_runner.environment()), `codex exec`'s stderr held only "Reading additional
input from stdin..." for these.

Captured the same way by the review of 14 Nov 2026 (~/tcs-work/scratch/review-ready/out/codex1), with ports and IDs as
printed: a stream that the endpoint started (HTTP 200) and ended with response.failed, whose error's message is a
JSON body, as a proxy passes on its upstream's. Codex retried it five times itself ("Reconnecting... <n>/5"), then
reported "stream disconnected before completion: <the message>", through `codex exec --json` (EXEC_STREAM_DETAIL,
FastAPI's {"detail": "Upstream request timed out"}; EXEC_STREAM_ERROR_TEXT, an "error" that is text) and its
app-server (APP_SERVER_STREAM_DETAIL: codexErrorInfo responseStreamDisconnected while it retried, then "other",
willRetry false). Without the JSON (a message in plain words) Codex reported the same words around it.
"""

import json

# The endpoint's own message in EXEC_400, APP_SERVER_400 and EXEC_400_INDENTED.
ENDPOINT_MESSAGE = "Unsupported parameter: 'reasoning.summary' is not supported with this model."
# The messages of EXEC_400_INPUT_512 (and APP_SERVER_400_INPUT_512), EXEC_400_INPUT_429 and the vLLM bodies.
INPUT_512_MESSAGE = ("Invalid 'input[512].content': string too long. Expected a string with maximum length 10485760, "
                     "but got a string with length 10485761 instead.")
INPUT_429_MESSAGE = ("Invalid 'input[429].content[0].text': empty string. Expected a string with minimum length 1, but "
                     "got an empty string instead.")
VLLM_MESSAGE = "'reasoning.summary' is not supported."

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

EXEC_400_FILTERED = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"The response was filtered due to the prompt '''
    r'''triggering Azure OpenAI's content management policy. Please modify your prompt and retry.\", \"type\": '''
    r'''\"invalid_request_error\", \"param\": \"prompt\", \"code\": \"content_filter\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"The response was filtered due to '''
    r'''the prompt triggering Azure OpenAI's content management policy. Please modify your prompt and retry.\", '''
    r'''\"type\": \"invalid_request_error\", \"param\": \"prompt\", \"code\": \"content_filter\"}}"}}''',
)
APP_SERVER_400_FILTERED = (
    r'''{"method":"error","params":{"error":{"message":"{\"error\": {\"message\": \"The response was filtered '''
    r'''due to the prompt triggering Azure OpenAI's content management policy. Please modify your prompt and '''
    r'''retry.\", \"type\": \"invalid_request_error\", \"param\": \"prompt\", \"code\": \"content_filter\"}}",'''
    r'''"codexErrorInfo":"other","additionalDetails":null,"misalignment":null},"willRetry":false,'''
    r'''"threadId":"01a1da7c-b3df-7740-8d71-e67a165e4446","turnId":"01a1da7c-b3fc-7023-946d-49b487c07245"},'''
    r'''"emittedAtMs":1794666968079}''',
    r'''{"method":"turn/completed","params":{"threadId":"01a1da7c-b3df-7740-8d71-e67a165e4446",'''
    r'''"turn":{"id":"01a1da7c-b3fc-7023-946d-49b487c07245","items":[],"itemsView":"notLoaded",'''
    r'''"status":"failed","error":{"message":"{\"error\": {\"message\": \"The response was filtered due to the '''
    r'''prompt triggering Azure OpenAI's content management policy. Please modify your prompt and retry.\", '''
    r'''\"type\": \"invalid_request_error\", \"param\": \"prompt\", \"code\": \"content_filter\"}}",'''
    r'''"codexErrorInfo":"other","additionalDetails":null,"misalignment":null},"startedAt":1794666968,'''
    r'''"completedAt":1794666968,"durationMs":18}},"emittedAtMs":1794666968080}''',
)

EXEC_400_INPUT_512 = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"Invalid 'input[512].content': string too long. '''
    r'''Expected a string with maximum length 10485760, but got a string with length 10485761 instead.\", '''
    r'''\"type\": \"invalid_request_error\", \"param\": \"input[512].content\", \"code\": '''
    r'''\"string_above_max_length\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"Invalid 'input[512].content': '''
    r'''string too long. Expected a string with maximum length 10485760, but got a string with length 10485761 '''
    r'''instead.\", \"type\": \"invalid_request_error\", \"param\": \"input[512].content\", \"code\": '''
    r'''\"string_above_max_length\"}}"}}''',
)
APP_SERVER_400_INPUT_512 = (
    r'''{"method":"error","params":{"error":{"message":"{\"error\": {\"message\": '''
    r'''\"Invalid 'input[512].content': string too long. Expected a string with maximum length 10485760, but '''
    r'''got a string with length 10485761 instead.\", \"type\": \"invalid_request_error\", \"param\": '''
    r'''\"input[512].content\", \"code\": \"string_above_max_length\"}}","codexErrorInfo":"other",'''
    r'''"additionalDetails":null,"misalignment":null},"willRetry":false,'''
    r'''"threadId":"01a1da7c-b4d1-7763-bbe5-89bbe6994596","turnId":"01a1da7c-b4db-7772-b197-144ac7d526bb"},'''
    r'''"emittedAtMs":1794666968305}''',
    r'''{"method":"turn/completed","params":{"threadId":"01a1da7c-b4d1-7763-bbe5-89bbe6994596",'''
    r'''"turn":{"id":"01a1da7c-b4db-7772-b197-144ac7d526bb","items":[],"itemsView":"notLoaded",'''
    r'''"status":"failed","error":{"message":"{\"error\": {\"message\": \"Invalid 'input[512].content': string '''
    r'''too long. Expected a string with maximum length 10485760, but got a string with length 10485761 '''
    r'''instead.\", \"type\": \"invalid_request_error\", \"param\": \"input[512].content\", \"code\": '''
    r'''\"string_above_max_length\"}}","codexErrorInfo":"other","additionalDetails":null,"misalignment":null},'''
    r'''"startedAt":1794666968,"completedAt":1794666968,"durationMs":20}},"emittedAtMs":1794666968306}''',
)
EXEC_400_INPUT_429 = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"Invalid 'input[429].content[0].text': empty '''
    r'''string. Expected a string with minimum length 1, but got an empty string instead.\", \"type\": '''
    r'''\"invalid_request_error\", \"param\": \"input[429].content[0].text\", \"code\": \"empty_string\"}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": '''
    r'''\"Invalid 'input[429].content[0].text': empty string. Expected a string with minimum length 1, but got '''
    r'''an empty string instead.\", \"type\": \"invalid_request_error\", \"param\": '''
    r'''\"input[429].content[0].text\", \"code\": \"empty_string\"}}"}}''',
)
EXEC_400_VLLM = (
    r'''{"type":"error","message":"{\"object\": \"error\", \"message\": \"'reasoning.summary' is not '''
    r'''supported.\", \"type\": \"BadRequestError\", \"param\": null, \"code\": 400}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"object\": \"error\", \"message\": \"'reasoning.summary' is '''
    r'''not supported.\", \"type\": \"BadRequestError\", \"param\": null, \"code\": 400}"}}''',
)
APP_SERVER_400_VLLM = (
    r'''{"method":"error","params":{"error":{"message":"{\"object\": \"error\", \"message\": '''
    r'''\"'reasoning.summary' is not supported.\", \"type\": \"BadRequestError\", \"param\": null, \"code\": '''
    r'''400}","codexErrorInfo":"other","additionalDetails":null,"misalignment":null},"willRetry":false,'''
    r'''"threadId":"01a1da7c-b632-7171-8d84-793fc0d596da","turnId":"01a1da7c-b63d-7583-8f24-6e41c21c90d6"},'''
    r'''"emittedAtMs":1794666968659}''',
    r'''{"method":"turn/completed","params":{"threadId":"01a1da7c-b632-7171-8d84-793fc0d596da",'''
    r'''"turn":{"id":"01a1da7c-b63d-7583-8f24-6e41c21c90d6","items":[],"itemsView":"notLoaded",'''
    r'''"status":"failed","error":{"message":"{\"object\": \"error\", \"message\": \"'reasoning.summary' is not '''
    r'''supported.\", \"type\": \"BadRequestError\", \"param\": null, \"code\": 400}","codexErrorInfo":"other",'''
    r'''"additionalDetails":null,"misalignment":null},"startedAt":1794666968,"completedAt":1794666968,'''
    r'''"durationMs":21}},"emittedAtMs":1794666968660}''',
)
EXEC_400_VLLM_WRAPPED = (
    r'''{"type":"error","message":"{\"error\": {\"message\": \"'reasoning.summary' is not supported.\", '''
    r'''\"type\": \"BadRequestError\", \"param\": null, \"code\": 400}}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": {\"message\": \"'reasoning.summary' is not '''
    r'''supported.\", \"type\": \"BadRequestError\", \"param\": null, \"code\": 400}}"}}''',
)
EXEC_400_VLLM_TOO_LONG = (
    r'''{"type":"error","message":"{\"object\": \"error\", \"message\": \"This model's maximum context length '''
    r'''is 32768 tokens. However, you requested 33792 tokens (32768 in the messages, 1024 in the completion). '''
    r'''Please reduce the length of the messages or completion.\", \"type\": \"BadRequestError\", \"param\": '''
    r'''null, \"code\": 400}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"object\": \"error\", \"message\": \"This model's maximum '''
    r'''context length is 32768 tokens. However, you requested 33792 tokens (32768 in the messages, 1024 in the '''
    r'''completion). Please reduce the length of the messages or completion.\", \"type\": \"BadRequestError\", '''
    r'''\"param\": null, \"code\": 400}"}}''',
)
EXEC_400_ERROR_TEXT = (
    r'''{"type":"error","message":"{\"error\": \"lab-model does not support tools\"}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"error\": \"lab-model does not support tools\"}"}}''',
)
EXEC_400_DETAIL = (
    r'''{"type":"error","message":"{\"detail\": \"Bad Request\"}"}''',
    r'''{"type":"turn.failed","error":{"message":"{\"detail\": \"Bad Request\"}"}}''',
)
EXEC_400_PLAIN = (
    r'''{"type":"error","message":"Bad Request"}''',
    r'''{"type":"turn.failed","error":{"message":"Bad Request"}}''',
)

# The words of the stream's error in EXEC_STREAM_DETAIL and APP_SERVER_STREAM_DETAIL.
STREAM_MESSAGE = "Upstream request timed out"
EXEC_STREAM_DETAIL = (
    r'''{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 2/5 (stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 3/5 (stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 4/5 (stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 5/5 (stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"})"}''',
    r'''{"type":"error","message":"stream disconnected before completion: {\"detail\": \"Upstream request timed '''
    r'''out\"}"}''',
    r'''{"type":"turn.failed","error":{"message":"stream disconnected before completion: {\"detail\": \"Upstream '''
    r'''request timed out\"}"}}''',
)
EXEC_STREAM_ERROR_TEXT = (
    r'''{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: {\"error\": '''
    r'''\"upstream connect error or disconnect/reset before headers\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 2/5 (stream disconnected before completion: {\"error\": '''
    r'''\"upstream connect error or disconnect/reset before headers\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 3/5 (stream disconnected before completion: {\"error\": '''
    r'''\"upstream connect error or disconnect/reset before headers\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 4/5 (stream disconnected before completion: {\"error\": '''
    r'''\"upstream connect error or disconnect/reset before headers\"})"}''',
    r'''{"type":"error","message":"Reconnecting... 5/5 (stream disconnected before completion: {\"error\": '''
    r'''\"upstream connect error or disconnect/reset before headers\"})"}''',
    r'''{"type":"error","message":"stream disconnected before completion: {\"error\": \"upstream connect error or '''
    r'''disconnect/reset before headers\"}"}''',
    r'''{"type":"turn.failed","error":{"message":"stream disconnected before completion: {\"error\": \"upstream '''
    r'''connect error or disconnect/reset before headers\"}"}}''',
)
APP_SERVER_STREAM_DETAIL = (
    r'''{"method":"error","params":{"error":{"message":"Reconnecting... 1/5","codexErrorInfo":'''
    r'''{"responseStreamDisconnected":{"httpStatusCode":null}},"additionalDetails":"stream disconnected before '''
    r'''completion: {\"detail\": \"Upstream request timed out\"}","misalignment":null},"willRetry":true,'''
    r'''"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37","turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},'''
    r'''"emittedAtMs":1794674661998}''',
    r'''{"method":"error","params":{"error":{"message":"Reconnecting... 2/5","codexErrorInfo":'''
    r'''{"responseStreamDisconnected":{"httpStatusCode":null}},"additionalDetails":"stream disconnected before '''
    r'''completion: {\"detail\": \"Upstream request timed out\"}","misalignment":null},"willRetry":true,'''
    r'''"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37","turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},'''
    r'''"emittedAtMs":1794674662187}''',
    r'''{"method":"error","params":{"error":{"message":"Reconnecting... 3/5","codexErrorInfo":'''
    r'''{"responseStreamDisconnected":{"httpStatusCode":null}},"additionalDetails":"stream disconnected before '''
    r'''completion: {\"detail\": \"Upstream request timed out\"}","misalignment":null},"willRetry":true,'''
    r'''"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37","turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},'''
    r'''"emittedAtMs":1794674662583}''',
    r'''{"method":"error","params":{"error":{"message":"Reconnecting... 4/5","codexErrorInfo":'''
    r'''{"responseStreamDisconnected":{"httpStatusCode":null}},"additionalDetails":"stream disconnected before '''
    r'''completion: {\"detail\": \"Upstream request timed out\"}","misalignment":null},"willRetry":true,'''
    r'''"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37","turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},'''
    r'''"emittedAtMs":1794674663435}''',
    r'''{"method":"error","params":{"error":{"message":"Reconnecting... 5/5","codexErrorInfo":'''
    r'''{"responseStreamDisconnected":{"httpStatusCode":null}},"additionalDetails":"stream disconnected before '''
    r'''completion: {\"detail\": \"Upstream request timed out\"}","misalignment":null},"willRetry":true,'''
    r'''"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37","turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},'''
    r'''"emittedAtMs":1794674664921}''',
    r'''{"method":"error","params":{"error":{"message":"stream disconnected before completion: {\"detail\": '''
    r'''\"Upstream request timed out\"}","codexErrorInfo":"other","additionalDetails":null,"misalignment":null},'''
    r'''"willRetry":false,"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37",'''
    r'''"turnId":"01a1daf2-1a58-7251-bfc1-bc431962d988"},"emittedAtMs":1794674668032}''',
    r'''{"method":"turn/completed","params":{"threadId":"01a1daf2-1a4c-74e3-b298-6a9bedb40e37",'''
    r'''"turn":{"id":"01a1daf2-1a58-7251-bfc1-bc431962d988","items":[],"itemsView":"notLoaded",'''
    r'''"status":"failed","error":{"message":"stream disconnected before completion: {\"detail\": \"Upstream '''
    r'''request timed out\"}","codexErrorInfo":"other","additionalDetails":null,"misalignment":null},'''
    r'''"startedAt":1794674661,"completedAt":1794674668,"durationMs":6054}},"emittedAtMs":1794674668033}''',
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
