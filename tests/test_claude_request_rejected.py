"""Claude Code's "API Error: 400" on an API connection: a request the endpoint refuses (owner's decision, 13 Nov 2026).

Claude Code 2.1.289 ran against a fake Anthropic endpoint on 127.0.0.1 that answered every request with HTTP 400
and Anthropic's error body ({"type": "error", "error": {"type": "invalid_request_error", "message": ...}}). It
reported the refusal as the two stream-json lines of CAPTURED_400 (a structured call's; the author's session gave the
same lines with its own IDs), with CAPTURED_STDERR on its error output, and exited with code 1. Its text is
"API Error: 400 <the endpoint's message>". An Anthropic-format API model's failure with that text is handled as an
OpenAI-format API model's 400 invalid_request_error is (workflow_runner.settings_failure(), RequestRejected): a
structured call ends after its first attempt, the author pauses with a message that says to change the connection's
settings (a job waiting for it continues once they change), a paused author's reply fails as a settings problem, and a
team member's assignment comes back to the coordinator as a failure with the reason.

Everything else stays retried every minute: the same endpoint's 400 for a conversation too long for the model and for a
key out of credit, which Claude Code words itself (CAPTURED_TOO_LONG, CAPTURED_CREDIT), 429, overload and 5xx, and the
same "API Error: 400" from a built-in Claude model (the subscription route). The stand-in CLI replays the captured
lines with its own session ID (tests/test_claude_cli.py); no test starts a model.
"""

import json
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import claude_cli, providers
from ui import server
import workflow_runner as runtime

import test_author_conversation as conversation_test
import test_claude_cli as claude_test
import test_decisions_jobs as jobs_test
import test_team_goal_loop as team_test

# The stream-json lines of Claude Code 2.1.289 for a structured call that its endpoint refused with HTTP 400,
# invalid_request_error, "max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens for
# claude-gw": an assistant message that reports the failed request, then the result.
CAPTURED_400 = (
    '{"type":"assistant","message":{"diagnostics":null,"id":"40525b1c-ca25-47ef-8147-2b3cdd43feb0",'
    '"container":null,"model":"<synthetic>","role":"assistant","stop_details":null,"stop_reason":"stop_sequence",'
    '"stop_sequence":"","type":"message","usage":{"output_tokens_details":null,"input_tokens":0,'
    '"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"server_tool_use":{"web_search_r'
    'equests":0,"web_fetch_requests":0},"service_tier":null,"cache_creation":{"ephemeral_1h_input_tokens":0,'
    '"ephemeral_5m_input_tokens":0},"inference_geo":null,"iterations":null,"speed":null,'
    '"fallback_credit":null},"content":[{"type":"text","text":"API Error: 400 max_tokens: 64000 > 32000,'
    ' which is the maximum allowed number of output tokens for claude-gw"}],"context_management":null},'
    '"parent_tool_use_id":null,"session_id":"1d895fca-d1f1-4319-9da2-8e737ee7734f","uuid":"e1813aec-bc36-4bb3-9285-'
    '5dd4c08a8c32","timestamp":"2026-11-13T21:20:50.918Z","error":"unknown","request_id":"req_fake00000000000000000'
    '002","is_api_error_message":true}',
    '{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"1d895fca-d1f1-4319-9da2-8e737ee7734f",'
    '"total_cost_usd":0,"usage":{"output_tokens_details":{"thinking_tokens":0},"input_tokens":0,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,"server_tool_use":{"web_search_r'
    'equests":0,"web_fetch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,'
    '"ephemeral_5m_input_tokens":0},"inference_geo":"","iterations":[],"speed":"standard",'
    '"fallback_credit":null},"modelUsage":{},"permission_denials":[],"terminal_reason":"api_error",'
    '"fast_mode_state":"off","fast_mode_disabled_reason":"sdk_opt_in_required","subagent_stats":{"spawned":0,'
    '"requested":{"background":0,"foreground":0,"unset":0},"started_in_background":0,'
    '"max_depth":0,"spawned_by_subagents":0,"completed":0,"failed":0,"killed":{"parent":0,'
    '"user":0,"system":0},"refused":{"depth_limit":0,"concurrency_limit":0,"budget":0},'
    '"by_type":{}},"is_error":true,"num_turns":1,"subtype":"success","api_error_status":400,'
    '"result":"API Error: 400 max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens for c'
    'laude-gw","type":"result","duration_ms":63,"uuid":"b456306d-7970-41de-9825-59f02b9aa821",'
    '"queued_turn_count":0,"result_index":0}',
)
# Its error output: both lines are ones it prints on every run (claude_cli.HARMLESS_STDERR).
CAPTURED_STDERR = (
    "⚠ Permission mode forced to default — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set (allowed_non_write_users "
    "hardening). Declare allowedTools explicitly, or set CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 to opt out.",
    '[claude-code:unrecognized_model] {"model":"claude-gw","query_source":"sdk"}',
)
# The result of the same endpoint's 400 invalid_request_error "prompt is too long: 250000 tokens > 200000 maximum".
CAPTURED_TOO_LONG = (
    '{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"75ac2d1c-cb0f-4c20-87a8-08d0a26bf26a",'
    '"total_cost_usd":0,"usage":{"output_tokens_details":{"thinking_tokens":0},"input_tokens":0,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,"server_tool_use":{"web_search_r'
    'equests":0,"web_fetch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,'
    '"ephemeral_5m_input_tokens":0},"inference_geo":"","iterations":[],"speed":"standard",'
    '"fallback_credit":null},"modelUsage":{},"permission_denials":[],"terminal_reason":"prompt_too_long",'
    '"fast_mode_state":"off","fast_mode_disabled_reason":"sdk_opt_in_required","subagent_stats":{"spawned":0,'
    '"requested":{"background":0,"foreground":0,"unset":0},"started_in_background":0,'
    '"max_depth":0,"spawned_by_subagents":0,"completed":0,"failed":0,"killed":{"parent":0,'
    '"user":0,"system":0},"refused":{"depth_limit":0,"concurrency_limit":0,"budget":0},'
    '"by_type":{}},"is_error":true,"num_turns":1,"subtype":"success","api_error_status":400,'
    '"result":"Prompt is too long · the request is ~250000 tokens (limit 200000) but this conversation is only ~151 '
    'tokens — the rest is system prompt, tool definitions, and attachment content. A single-exchange conversation can'
    'not be compacted; reduce attached files/tools or start with less context.","type":"result","duration_ms":32,'
    '"uuid":"52d5d0e0-39e3-46b7-a670-edded0ce760d","queued_turn_count":0,"result_index":0}'
)
# The result of the same endpoint's 400 invalid_request_error "Your credit balance is too low to access the Anthropic
# API. Please go to Plans & Billing to upgrade or purchase credits."
CAPTURED_CREDIT = (
    '{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"4f0f1d9d-173f-4e9d-aa84-c83397421f1f",'
    '"total_cost_usd":0,"usage":{"output_tokens_details":{"thinking_tokens":0},"input_tokens":0,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,"server_tool_use":{"web_search_r'
    'equests":0,"web_fetch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,'
    '"ephemeral_5m_input_tokens":0},"inference_geo":"","iterations":[],"speed":"standard",'
    '"fallback_credit":null},"modelUsage":{},"permission_denials":[],"terminal_reason":"api_error",'
    '"fast_mode_state":"off","fast_mode_disabled_reason":"sdk_opt_in_required","subagent_stats":{"spawned":0,'
    '"requested":{"background":0,"foreground":0,"unset":0},"started_in_background":0,'
    '"max_depth":0,"spawned_by_subagents":0,"completed":0,"failed":0,"killed":{"parent":0,'
    '"user":0,"system":0},"refused":{"depth_limit":0,"concurrency_limit":0,"budget":0},'
    '"by_type":{}},"is_error":true,"num_turns":1,"subtype":"success","api_error_status":400,'
    '"result":"Credit balance is too low","type":"result","duration_ms":33,"uuid":"0660d67f-fd3e-4416-8cab-'
    '6b570fdc0301","queued_turn_count":0,"result_index":0}'
)
ENDPOINT_MESSAGE = "max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens for claude-gw"
# What the job says for the Anthropic-format connection "Gateway" (the label of claude_test's API model).
REJECTED = (f"{runtime.REQUEST_REJECTED_LABEL}: Gateway refuses the requests this job sends as invalid (API Error: 400 "
            f"{ENDPOINT_MESSAGE}). A retry cannot fix this: change its API connection's settings under Models & API "
            "(or the endpoint); a job waiting for it continues once its settings change.")


def replayed(line):
    """A captured line as the stand-in CLI writes it, with the stand-in's own session ID."""

    message = json.loads(line)
    message.pop("session_id", None)
    return {"emit": message}


def refused(*before, result=CAPTURED_400[1], exits=True):
    """A turn of the stand-in CLI that ends as Claude Code ended the captured one (``result``: its last line)."""

    return ["init", *before, replayed(CAPTURED_400[0]), replayed(result),
            *({"stderr": line} for line in CAPTURED_STDERR), *([{"exit": 1}] if exits else [])]


def runner_text(result_line, prefix="Claude review failed: "):
    """The failure text the runner makes of a captured result line (claude_cli.failure_detail(), keys removed)."""

    return prefix + claude_cli.failure_detail(json.loads(result_line), CAPTURED_STDERR, "", runtime)


class CapturedTextTests(jobs_test.Isolated):
    """settings_failure() on the runner's text of the captured lines, for an Anthropic-format API model."""

    def model(self):
        return self.api_model(format="anthropic", baseUrl="https://gateway.example.com", model="claude-gw")["id"]

    def test_the_captured_400_is_a_request_the_endpoint_refuses(self):
        model = self.model()
        text = runner_text(CAPTURED_400[1])
        # The harmless error output is left out: the text is Claude Code's own line and the API status.
        self.assertEqual(text, f"Claude review failed: API Error: 400 {ENDPOINT_MESSAGE}\nAPI status 400.")
        problem = runtime.settings_failure(runtime.Error(text), model)
        self.assertIsInstance(problem, runtime.RequestRejected)
        self.assertEqual(str(problem), REJECTED)
        self.assertFalse(runtime.retryable_failure(problem))
        # The author's and a paused author's reply turn say the same in their own words.
        for prefix in ("Claude author turn failed: ", "Claude reply turn failed: "):
            self.assertEqual(str(runtime.settings_failure(runtime.Error(runner_text(CAPTURED_400[1], prefix)), model)),
                             REJECTED)
        # A team member's failed assignment gives the coordinator the same reason.
        self.assertEqual(runtime.member_problem(runtime.Error(text), model), REJECTED)

    def test_the_same_text_from_a_built_in_claude_model_stays_retried(self):
        text = runner_text(CAPTURED_400[1])
        self.assertIsNone(runtime.settings_failure(runtime.Error(text), "claude-opus-5-5"))
        self.assertIsNone(runtime.member_problem(runtime.Error(text), "claude-opus-5-5"))
        self.assertTrue(runtime.retryable_failure(runtime.Error(text)))

    def test_a_conversation_too_long_a_key_out_of_credit_429_overload_and_5xx_stay_retried(self):
        model = self.model()
        texts = [runner_text(CAPTURED_TOO_LONG), runner_text(CAPTURED_CREDIT)]
        # As Claude Code reports them (tests/test_model_retry.py), and an endpoint's 400 that names a passing cause.
        texts += ["Claude review failed: API Error: 529 Overloaded\nAPI status 529.",
                  'Claude review failed: API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":'
                  '"Overloaded"}}\nAPI status 529.',
                  "Claude review failed: API Error: 429 rate_limit_error\nAPI status 429.",
                  "Claude review failed: API Error: 500 Internal server error\nAPI status 500.",
                  "Claude review failed: API Error: 503 upstream unavailable\nAPI status 503.",
                  "Claude review failed: API Error: 400 the model is overloaded, try again later\nAPI status 400."]
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), model))
                self.assertIsNone(runtime.member_problem(runtime.Error(text), model))
        # A key the endpoint refuses stays retried too (the job page says the key was rejected); a team member's
        # assignment comes back as an authentication failure.
        for status in (401, 403):
            text = (f"Claude review failed: Failed to authenticate. API Error: {status} invalid x-api-key\n"
                    f"API status {status}.")
            with self.subTest(text=text):
                self.assertIsNone(runtime.settings_failure(runtime.Error(text), model))
                problem = runtime.member_problem(runtime.Error(text), model)
                self.assertTrue(problem.startswith("authentication failed: "), problem)


class ClaudeCodeRunTests(claude_test.FakeClaudeCase):
    """The stand-in CLI replays the captured lines in a structured call, the author's session and a reply turn."""

    def test_a_structured_call_ends_after_its_first_attempt(self):
        model = self.api_model()
        self.script([refused(), ["init", {"structured": {"statement": "S", "notes": ""}}]])
        with self.assertRaises(runtime.RequestRejected) as raised:
            runtime.structured("Review.", claude_test.SCHEMA, "review", model=model)
        self.assertEqual(str(raised.exception), REJECTED)
        self.assertEqual(len(self.records("start")), 1)
        self.assertNotIn(runtime.RETRY_LABEL, self.labels())

    def test_a_built_in_models_structured_call_is_retried(self):
        self.script([refused(), ["init", {"structured": {"statement": "S", "notes": ""}}]])
        result, _ = runtime.structured("Review.", claude_test.SCHEMA, "review", model="claude-opus-5-5")
        self.assertEqual(result, {"statement": "S", "notes": ""})
        self.assertEqual(len(self.records("start")), 2)

    def test_the_author_pauses_with_the_message_and_no_retry(self):
        model = self.api_model()
        self.script([refused("echo", exits=False)])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            with self.assertRaises(runtime.WorkflowPaused) as paused:
                next(self.goal(model=model))
        self.assertEqual(paused.exception.reason, REJECTED)
        self.assertIsNone(paused.exception.retry)
        status, = [event for event in self.events
                   if event["kind"] == "status" and event.get("label") == "Author paused"]
        self.assertEqual(status["text"], REJECTED)
        self.assertEqual(len(self.records("start")), 1)

    def test_a_built_in_model_author_is_retried(self):
        self.script([refused("echo", exits=False)])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal())
        self.assertTrue(paused.exception.reason.startswith(f"Claude author turn failed: API Error: 400 "
                                                           f"{ENDPOINT_MESSAGE}"), paused.exception.reason)
        self.assertIsNotNone(paused.exception.retry)

    def test_a_paused_authors_reply_fails_as_a_settings_problem(self):
        model = self.api_model()
        session = "6a1d2f8e-3c4b-4d5e-8f9a-0b1c2d3e4f50"
        self.script([refused("echo", exits=False), conversation_test.claude_reply("Not reached.")])
        code = runtime.run_author_conversation(conversation_test.WORKFLOW, {
            "author_model": model, "goal_thread_id": session, "goal_cwd": str(self.run_dir)},
            {"schemaVersion": 1, "id": conversation_test.CONVERSATION_ID, "kind": "message", "text": "Which step?",
             "notice": ""})
        self.assertEqual(code, 0)
        failed, = [event for event in self.events if event.get("label") == "Author reply failed"]
        self.assertEqual((failed["failure"], failed["final"], failed["text"]), ("configuration", True, REJECTED))
        self.assertEqual(len(self.records("start")), 1)
        self.assertFalse([event for event in self.events if event["kind"] == "conversation_reply"])


class TeamMemberTests(team_test.TeamCase):
    """A team member on an Anthropic-format connection: the coordinator gets the failed assignment with the reason."""

    def anthropic_model(self):
        return providers.save_api_model({"format": "anthropic", "baseUrl": "https://lab.example.com",
                                         "headerName": "x-api-key", "model": "claude-gw", "apiKey": team_test.API_KEY,
                                         "label": "Lab Claude"})["id"]

    def test_the_assignment_fails_with_the_reason_and_is_not_retried(self):
        model = self.anthropic_model()
        self.scenario.write_text(json.dumps({"turns": [refused(), ["init", {"result": {"result": "Not reached."}}]]}))
        crew = self.crew([{"model": model, "effort": "high"}])
        crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
        report, = self.collect(crew, 1)
        self.assertIn("failed the assignment", report)
        self.assertIn(REJECTED.replace("Gateway", "Lab Claude"), report)
        self.assertEqual(len(self.claude_starts()), 1)
        self.assertFalse([event for event in self.events if event["kind"] == "diagnostic"
                          and "retrying" in event.get("text", "")])

    def test_a_built_in_claude_member_is_retried(self):
        self.scenario.write_text(json.dumps({"turns": [refused(), ["init", {"emit": {
            "type": "assistant", "parent_tool_use_id": None, "uuid": "m1",
            "message": {"content": [{"type": "text", "text": "Lemma A holds."}]}}},
            {"result": {"result": "Lemma A holds."}}]]}))
        crew = self.crew([{"model": "claude-opus-5-5", "effort": "high"}])
        crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
        report, = self.collect(crew, 1)
        self.assertIn("REPORT:\nLemma A holds.", report)
        self.assertEqual(len(self.claude_starts()), 2)


class WaitingJobTests(jobs_test.Isolated):
    """A job paused on the message waits for its Anthropic-format connection to change, then continues by itself."""

    def test_it_needs_attention_and_resumes_once_the_connection_changes(self):
        model = self.api_model(format="anthropic", baseUrl="https://gateway.example.com", model="claude-gw")["id"]
        problem = runtime.settings_failure(runtime.Error(runner_text(CAPTURED_400[1], "Claude author turn failed: ")),
                                           model)
        app = self.paused_job(reason=str(problem), authorModel=model)
        self.assertEqual(app.state["autoResumeAt"], 0)
        attention = app.attention()
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]),
                         ("request", "Endpoint refuses the requests", False))
        resumed = []
        self.assertFalse(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9))
        time.sleep(1.1)  # updatedAt has one-second resolution.
        providers.save_api_model({"id": model, "maxOutputTokens": 32000})
        with mock.patch.object(server, "require_model_access"):
            self.assertTrue(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9 + 60))
        self.assertEqual(resumed, [1])


if __name__ == "__main__":
    unittest.main()
