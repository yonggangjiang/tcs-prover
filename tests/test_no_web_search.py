"""The warning at the start of the author's run when its search is live but Codex gives its model no web search tool.

Codex 0.160 (its source at tag rust-v0.160.0 and its installed binary against a local stand-in for the model API)
never offers a "Responses Lite" model such as gpt-6.1-sol its hosted web_search tool, and offers its own web search
(web.run) only through its built-in OpenAI provider or a provider that asks for it, which TCS Prover's provider for API
connections does not. Offline: the author's session is a stub; no CLI or model starts.
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers

import test_author_coordinator as coordinator_test

WORKFLOW = Path(runtime.__file__).parent / "workflows/author_critic.yaml"


def warning_record():
    """The record the runner makes as a live run's author starts on gpt-6.1-sol through an API connection "Lab
    gateway" (the page's tests read it as a job's transcript)."""

    records = []
    with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {
            providers.STORE_ENV: str(Path(folder) / "state" / "api.json"), "TCS_PROVER_RETRIEVAL_MODE": "live"}), \
            mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields:
                              records.append({"kind": kind, "stage": stage, **fields, "time": "2026-11-13T12:00:00Z"})):
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        model = providers.save_api_model({"format": "openai", "baseUrl": "https://lab.example.com/v1",
                                          "model": "gpt-6.1-sol", "headerName": "Authorization",
                                          "apiKey": "sk-lab-0123456789abcdef", "label": "Lab gateway"})["id"]
        runtime.note_no_web_search("solve", "author", model, directory=folder)
    record, = records
    return record


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.directory = self.root / "run"
        self.directory.mkdir()
        environment = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api.json"),
                                                   "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def api(self, model, label="Lab gateway", fmt="openai"):
        return providers.save_api_model({"format": fmt, "baseUrl": "https://lab.example.com/v1", "model": model,
                                         "headerName": "Authorization", "apiKey": "sk-lab-0123456789abcdef",
                                         "label": label})["id"]


class RuleTests(Case):
    def test_codex_offers_no_search_tool_to_a_lite_model_through_an_api_connection(self):
        for model, expected in (
                # Built-in GPT models run on Codex's built-in OpenAI provider: its own web search.
                ("gpt-6.1-sol", True), ("gpt-6-luna", True),
                # Claude Code runs these; this rule is Codex's.
                ("claude-opus-5-5", None)):
            with self.subTest(model=model):
                self.assertIs(runtime.codex_web_search_tool(model), expected)
        for name, expected in (
                # Responses Lite in Codex 0.160's catalog, also by the longest name a model's name starts with, or
                # after one "<namespace>/".
                ("gpt-6.1-sol", False), ("gpt-6.1-sol-2026-11-01", False), ("openai/gpt-6.1-sol", False),
                ("gpt-5.6-terra", False), ("codex-auto-review", False),
                # Not Lite: Codex sends them its hosted web_search tool.
                ("gpt-5.5", True), ("gpt-5.5-pro", True), ("gpt-6.1", True), ("lab-model", True),
                ("a/b/gpt-6.1-sol", True), ("open ai/gpt-6.1-sol", True)):
            with self.subTest(name=name):
                self.assertIs(runtime.codex_web_search_tool(self.api(name)), expected)
        self.assertIsNone(runtime.codex_web_search_tool(self.api("claude-opus-5-5", fmt="anthropic")))
        self.assertIsNone(runtime.codex_web_search_tool("api:nothing-here"))

    def test_a_provider_that_asks_for_codex_s_own_search_gets_it(self):
        model = self.api("gpt-6.1-sol")
        arguments = runtime.provider_arguments(model)
        # TCS Prover's provider today: named "TCS Prover", no supports_standalone_web_search, no actor header.
        self.assertIn(f'model_providers.{runtime.API_PROVIDER}.name="TCS Prover"', arguments)
        self.assertFalse(any("supports_standalone_web_search" in argument for argument in arguments))
        for extra in ("supports_standalone_web_search=true", 'name="OpenAI"',
                      'http_headers={"User-Agent"="x","x-openai-actor-authorization"="token"}'):
            with self.subTest(extra=extra), mock.patch.object(
                    runtime, "provider_arguments",
                    return_value=[*arguments, "-c", f"model_providers.{runtime.API_PROVIDER}.{extra}"]):
                self.assertIs(runtime.codex_web_search_tool(model), True)


class WarningTests(Case):
    """The warning as the author's run starts (_execute; the session itself is a stub)."""

    def setUp(self):
        super().setUp()
        self.workflow = runtime.load_workflow(WORKFLOW)
        handoff = mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True)
        handoff.start()
        self.addCleanup(handoff.stop)

    def run_author(self, **options):
        events = []

        def session(module, prompt, **kwargs):
            events.append({"kind": "session"})
            yield {"outcome": "done", "output": "A complete inline proof."}

        with mock.patch.object(runtime, "goal_session", side_effect=session), \
                mock.patch.object(claude_cli, "goal_session", side_effect=session), \
                mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields:
                                  events.append({"kind": kind, "stage": stage, **fields})):
            runtime._execute(self.workflow, {"statement": "Prove S."},
                             {"workflow_stages": coordinator_test.AUTHOR_ONLY, "goal_cwd": str(self.directory),
                              **options}, self.workflow["prompts"])
        warnings = [event for event in events if event.get("label") == runtime.NO_WEB_SEARCH_LABEL]
        return events, warnings

    def test_a_lite_model_through_an_api_connection_says_it_has_no_web_search_once_as_the_author_starts(self):
        model = self.api("gpt-6.1-sol")
        events, warnings = self.run_author(author_model=model, author_subagents=0)
        warning, = warnings
        self.assertEqual(warning["kind"], "diagnostic")  # Pinned and shown as a warning on the job page.
        self.assertEqual((warning["stage"], warning["node"], warning["noWebSearch"], warning["models"]),
                         ("solve", "author", True, ["gpt-6.1-sol"]))
        self.assertEqual(warning["text"], 'Web search is on for this job, but the proof author\'s model has no web '
                                          'search on its connection: gpt-6.1-sol (API connection "Lab gateway"). Codex '
                                          'gives this model no search tool through an API connection, and web search '
                                          'only through its built-in OpenAI provider (the Codex CLI source). The run '
                                          'goes on without web search.')
        # Before the author's session starts.
        self.assertLess(events.index(warning), next(index for index, event in enumerate(events)
                                                    if event["kind"] == "session"))

    def test_a_team_member_without_web_search_is_named_too(self):
        lab = self.api("gpt-6-luna")
        _, warnings = self.run_author(author_team="tcs", author_subagents=3, author_subagent_models=[
            {"model": "gpt-6.1-sol", "effort": "high"}, {"model": lab, "effort": "low"},
            {"model": "claude-opus-5-5", "effort": "max"}])
        warning, = warnings
        self.assertEqual(warning["models"], ["gpt-6-luna"])
        self.assertIn('but author subagent 2\'s model has no web search on its connection: gpt-6-luna (API '
                      'connection "Lab gateway").', warning["text"])
        # An API coordinator and members: one warning that names each model once, with who runs it.
        other = self.api("gpt-6.1-sol", label="Other gateway")
        _, warnings = self.run_author(author_model=other, author_team="tcs", author_subagents=3,
                                      author_subagent_models=[{"model": "gpt-6.1-sol", "effort": "high"},
                                                              {"model": lab, "effort": "low"},
                                                              {"model": lab, "effort": "high"}])
        warning, = warnings
        self.assertEqual(warning["models"], ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertIn('but these models have no web search on their connection: the author coordinator\'s model, '
                      'gpt-6.1-sol (API connection "Other gateway"); the model of author subagents 2 and 3, gpt-6-luna '
                      '(API connection "Lab gateway"). Codex gives these models no search tool', warning["text"])
        # Same as the coordinator: its model, named once.
        _, warnings = self.run_author(author_model=other, author_team="tcs", author_subagents=3)
        warning, = warnings
        self.assertIn('but the model of the author coordinator and author subagents 1, 2 and 3 has no web search on its '
                      'connection: gpt-6.1-sol (API connection "Other gateway").', warning["text"])

    def test_no_warning_when_the_model_can_search_or_the_run_does_not_search(self):
        for name, options in (
                ("built-in GPT", {"author_model": "gpt-6.1-sol"}),
                ("Claude Code", {"author_model": "claude-opus-5-5"}),
                ("hosted search", {"author_model": self.api("gpt-5.5")}),
                ("Codex's own search", {"author_model": self.api("gpt-6.1-sol"), "opt_in": True}),
                ("search disabled", {"author_model": self.api("gpt-6.1-sol"), "mode": "disabled"})):
            with self.subTest(name):
                arguments = runtime.provider_arguments
                extra = (["-c", f"model_providers.{runtime.API_PROVIDER}.supports_standalone_web_search=true"]
                         if options.pop("opt_in", False) else [])
                with mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": options.pop("mode", "live")}), \
                        mock.patch.object(runtime, "provider_arguments",
                                          side_effect=lambda model: [*arguments(model), *extra]):
                    _, warnings = self.run_author(author_subagents=0, **options)
                self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
