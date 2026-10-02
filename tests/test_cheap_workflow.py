"""Offline tests of the cost-optimized author/critic workflow; no model calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from test_goal_runtime import Runtime, success
from test_workflow_runner import workspace

ROOT = Path(runtime.__file__).parent
CHEAP = ROOT / "workflows/author_critic_cheap.yaml"
CLEAN_UP = ROOT / "workflows/clean_up.yaml"
PROOF = "Every allowed case follows by the explicit argument supplied here."
LATEX = r"\documentclass{article}\begin{document}An explicit argument.\end{document}"
UI_OPTIONS = {"author_effort": "ultra", "critic_effort": "ultra", "writer_effort": "ultra",
              "effort": "ultra", "critic_rounds": 2}


def screen(verdict="clean", bugs=""):
    return {"verdict": verdict, "bugs": bugs}


def audit(verdict="no_issues", report=""):
    return {"verdict": verdict, "report": report}


def judge(verdict="pass", edits="none", solution="", bugs=""):
    return {"verdict": verdict, "edits": edits, "solution": solution, "bugs": bugs}


class Critic:
    """Scripted structured calls, recognised by the response schema."""

    def __init__(self, screens=(), judges=(), audit_value=None):
        self.screens, self.judges = list(screens), list(judges)
        self.audit_value = audit_value or audit()
        self.calls = []

    def __call__(self, prompt, schema, stage, **settings):
        fields = set(schema["properties"])
        if "latex" in fields:
            kind, value = "final", {"latex": LATEX}
        elif "edits" in fields:
            kind, value = "judge", self.judges.pop(0)
        elif "report" in fields:
            kind, value = "audit", self.audit_value
        else:
            kind, value = "screen", self.screens.pop(0)
        self.calls.append((kind, prompt, settings))
        runtime.validate_json_schema(value, schema)
        return value, json.dumps(value)

    def kinds(self):
        return [kind for kind, _, _ in self.calls]


class WorkflowDefinitionTests(unittest.TestCase):
    def test_loads_and_prepares_in_both_file_modes_without_simple_variants(self):
        workflow = runtime.load_workflow(CHEAP)
        self.assertFalse([name for name in workflow["prompts"] if name.endswith("_simple")])
        for managed in (True, False):
            prompts = runtime.prepare(workflow, {**workflow["options"], **UI_OPTIONS, "file_management": managed})
            self.assertEqual(prompts["author"].count("[STATEMENT]"), 1)
        self.assertEqual(set(workflow["nodes"]), {"author", "critic", "critic_panel"})
        self.assertEqual(workflow["nodes"]["critic"]["stage"], "critic")
        self.assertEqual(workflow["nodes"]["critic_panel"]["stage"], "critic")
        self.assertEqual(workflow["nodes"]["critic_panel"]["next"]["fixed"]["option"], "critic_rounds")
        self.assertIn("checkpoint", workflow["nodes"]["author"]["lifecycle"])

    def test_every_structured_request_has_a_long_timeout_and_no_subagents(self):
        nodes = runtime.load_workflow(CHEAP)["nodes"]
        requests = [nodes["critic"], nodes["critic_panel"], nodes["critic_panel"]["parallel"]]
        for request in requests:
            self.assertGreaterEqual(request["provider_options"]["openai"]["timeout"], 1800)
            self.assertNotIn("multi_agent", request.get("features", []))

    def test_ultra_keeps_its_per_request_depth_without_delegation(self):
        workflow = runtime.load_workflow(CHEAP)
        options = {**workflow["options"], **UI_OPTIONS}
        nodes = workflow["nodes"]
        panel = {"role": nodes["critic_panel"]["role"], **nodes["critic_panel"]["parallel"]}
        for node in (nodes["author"], nodes["critic"], nodes["critic_panel"], panel):
            self.assertEqual(runtime._settings(node, options)["effort"], "xhigh")
        self.assertEqual(runtime._structured_options(panel, options)["effort"], "xhigh")
        self.assertEqual(runtime._settings(nodes["author"], {**options, "author_effort": "high"})["effort"], "high")
        # Codex sends ultra as max for models without a multi-agent effort.
        self.assertEqual(runtime._settings(nodes["critic"], {**options, "critic_model": "gpt-5.6-sol"})["effort"], "max")
        provider_ultra = {**nodes["critic"], "provider_options": {"openai": {"effort": "ultra"}}}
        self.assertEqual(runtime._structured_options(provider_ultra, options)["effort"], "xhigh")
        standard = runtime.load_workflow(ROOT / "workflows/author_critic.yaml")
        self.assertEqual(standard["options"], {})
        self.assertEqual(runtime._settings(standard["nodes"]["author"], UI_OPTIONS)["effort"], "ultra")

    def test_workflow_options_are_defaults_below_cli_and_do_not_leak(self):
        seen = []
        def execute(workflow, state, options, prompts):
            seen.append(dict(options))
            return state
        with patch.object(runtime, "_execute", side_effect=execute):
            runtime.execute_workflows([CHEAP, CLEAN_UP], {"statement": "Task"},
                                      {**UI_OPTIONS, "checkpoint_tokens": 7})
        self.assertEqual(seen[0]["ultra_effort"], {"gpt-6-astra": "xhigh", "default": "max"})
        self.assertEqual(seen[0]["checkpoint_tokens"], 7)
        self.assertEqual(seen[0]["subagent_threads"], 1)
        self.assertNotIn("ultra_effort", seen[1])
        self.assertNotIn("codex_config", seen[1])

    def test_invalid_workflow_options_are_rejected(self):
        workflow = runtime.load_workflow(CHEAP)
        invalid = [{"unknown": 1}, {"checkpoint_tokens": True}, {"checkpoint_tokens": -1},
                   {"continuation_steer": 1}, {"ultra_effort": "ultra"}, {"subagent_effort": "huge"},
                   {"ultra_effort": {"gpt-6-astra": "ultra"}}, {"ultra_effort": {"other-model": "xhigh"}},
                   {"ultra_effort": {}}, {"codex_config": {"a.b": True}}, {"codex_config": {"a b": 1}},
                   {"codex_config": {"a": None}}, {"codex_config": {"a": 2**63}}, {"codex_config": []},
                   {"codex_config": {"model": "gpt-6-astra"}}, {"codex_config": {"agents": {"enabled": True}}},
                   {"codex_config": {"features": {"multi_agent": False}}}, {"codex_config": {"tools": {"web_search": False}}}, []]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workflow.yaml"
            for options in invalid:
                with self.subTest(options=options):
                    data = {"options": options, "prompts": workflow["prompts"], "nodes": workflow["nodes"]}
                    path.write_text(runtime.yaml.safe_dump(data), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        runtime.load_workflow(path)
            path.write_text(runtime.yaml.safe_dump({**data, "extra": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                runtime.load_workflow(path)

    def test_execute_uses_the_same_precedence_and_checks_set_values(self):
        seen = []
        with patch.object(runtime, "_execute", side_effect=lambda w, state, options, prompts: seen.append(options) or state):
            runtime.execute(CHEAP, {"statement": "x"}, options={"subagent_threads": 5})
            with self.assertRaises(ValueError):
                runtime.execute(CHEAP, {"statement": "x"}, options={"codex_config": "text"})
            with self.assertRaises(ValueError):
                runtime.execute_workflows([CHEAP], {"statement": "x"}, {"ultra_effort": "ultra"})
        self.assertEqual(seen[0]["subagent_threads"], 5)
        self.assertEqual(seen[0]["ultra_effort"], {"gpt-6-astra": "xhigh", "default": "max"})

    def test_toml_strings_keep_unicode_and_escape_controls(self):
        self.assertEqual(runtime._toml_value("π \U0001F600 \x7f \n"), '"π \U0001F600 \\u007f \\n"')

    def test_codex_config_flattens_to_toml_overrides(self):
        arguments = runtime.codex_config_arguments(
            {"skills": {"include_instructions": False}, "personality": "none", "limit": 3,
             "ratio": 0.5, "names": ["a", 'b"c'], "path": "C:\\x"})
        self.assertEqual(arguments, [
            "-c", "skills.include_instructions=false", "-c", 'personality="none"', "-c", "limit=3",
            "-c", "ratio=0.5", "-c", 'names=["a", "b\\"c"]', "-c", 'path="C:\\\\x"'])

    def test_structured_calls_without_multi_agent_cannot_delegate(self):
        self.assertIn("agents.enabled=false", runtime.structured_tool_arguments("final", []))
        self.assertNotIn("agents.enabled=false", runtime.structured_tool_arguments("critic", ["multi_agent"]))


class CheapPipelineTests(unittest.TestCase):
    def setUp(self):
        self.feedback = []
        self.revisions = [PROOF + " revised"]

        def author(runtime_module, prompt, **kwargs):
            self.author_kwargs = kwargs
            output = PROOF
            while True:
                rejection = yield {"outcome": "done", "output": output}
                self.feedback.append(rejection)
                output = self.revisions.pop(0)

        patcher = patch.object(runtime, "goal_session", side_effect=author)
        self.session = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cheap(self, critic, state=None, options=None, chain=False):
        events = []
        paths = [CHEAP, CLEAN_UP] if chain else [CHEAP]
        with workspace() as directory, patch.object(runtime, "structured", side_effect=critic), \
                patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}), \
                patch.object(runtime, "emit", side_effect=lambda *args, **kwargs: events.append((args, kwargs))):
            result = runtime.execute_workflows(paths, state or {"statement": "Task"}, {**UI_OPTIONS, **(options or {})})
            saved = (directory / "saved-candidate.md").read_text().strip()
        return result, events, saved

    def test_clean_candidate_gets_three_reads_and_no_copied_proof(self):
        critic = Critic(screens=[screen()], judges=[judge()])
        result, events, saved = self.run_cheap(critic, chain=True)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge", "final"])
        self.assertEqual(result["solution"], PROOF)
        self.assertEqual(result["output"], LATEX)
        self.assertEqual(saved, PROOF)
        for kind, prompt, settings in critic.calls[:4]:
            self.assertIn(PROOF, prompt)
            self.assertEqual(settings["effort"], "xhigh")
            self.assertEqual(settings["timeout"], 3600)
            self.assertIn("skills.include_instructions=false", settings["config_overrides"])
            self.assertEqual(settings.get("features", []), [])
        self.assertNotIn("config_overrides", critic.calls[4][2])
        labels = [fields.get("label") for _, fields in events]
        self.assertEqual(labels.count("Critic approved"), 1)
        report = next(fields["report"] for args, fields in events if args[0] == "critic_result")
        self.assertEqual(report, {"verdict": "pass", "solution": PROOF, "bugs": ""})

    def test_fatal_first_audit_returns_to_author_without_the_panel(self):
        critic = Critic(screens=[screen("fatal", "Lemma 2 fails for n = 1."), screen()], judges=[judge()])
        result, events, _ = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "screen", "audit", "audit", "judge"])
        self.assertEqual(self.session.call_count, 1)
        self.assertEqual(self.feedback[0]["bugs"], "Lemma 2 fails for n = 1.")
        self.assertEqual(self.feedback[0]["round"], 1)
        self.assertEqual(self.feedback[0]["candidate_note"], "is your last final answer (read the file if that answer is no longer in your context)")
        self.assertEqual(result["output"], PROOF + " revised")
        rejected = next(fields["report"] for args, fields in events if args[0] == "critic_result")
        self.assertEqual(rejected, {"verdict": "reject", "solution": PROOF, "bugs": "Lemma 2 fails for n = 1."})

    def test_fatal_verdict_without_bugs_is_audited_instead_of_rejected(self):
        critic = Critic(screens=[screen("fatal", " ")], judges=[judge()])
        result, _, _ = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge"])
        self.assertEqual(result["output"], PROOF)

    def test_substantive_edits_rerun_the_panel_up_to_the_round_limit(self):
        edits = [judge(edits="substantive", solution=PROOF + " fix" * n) for n in (1, 2, 3)]
        critic = Critic(screens=[screen("fixable", "Define f.")], judges=edits)
        result, events, saved = self.run_cheap(critic, options={"critic_rounds": 3})
        self.assertEqual(critic.kinds(), ["screen"] + ["audit", "audit", "judge"] * 3)
        self.assertIn("Define f.", critic.calls[3][1])
        self.assertIn(PROOF + " fix", critic.calls[4][1])
        self.assertNotIn("Define f.", critic.calls[6][1])
        self.assertEqual(result["output"], PROOF + " fix fix fix")
        self.assertEqual(saved, result["output"])
        self.assertEqual([fields.get("label") for _, fields in events].count("Critic approved"), 1)

    def test_harmless_edits_pass_without_another_round_and_are_not_applied(self):
        # Cosmetic rewrites are never applied, so no unaudited text replaces the reviewed proof.
        critic = Critic(screens=[screen()], judges=[judge(edits="harmless", solution=PROOF + " (typo fixed)")])
        result, _, _ = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge"])
        self.assertEqual(result["output"], PROOF)

    def test_placeholder_text_under_no_edits_keeps_the_reviewed_proof(self):
        critic = Critic(screens=[screen()], judges=[judge(solution="N/A")])
        result, _, saved = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge"])
        self.assertEqual(result["output"], PROOF)
        self.assertEqual(saved, PROOF)

    def test_harmless_label_without_full_text_keeps_the_reviewed_proof(self):
        for text in ("", PROOF[:20] + " [... rest unchanged ...]"):
            with self.subTest(text=text):
                critic = Critic(screens=[screen()], judges=[judge(edits="harmless", solution=text)])
                result, _, _ = self.run_cheap(critic)
                self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge"])
                self.assertEqual(result["output"], PROOF)

    def test_substantive_fix_with_abridged_text_is_rejected_with_the_audits(self):
        critic = Critic(screens=[screen(), screen()], judges=[judge(edits="substantive", solution="Short."), judge()],
                        audit_value=audit("issues", "Case n = 0 is missing."))
        result, _, _ = self.run_cheap(critic)
        self.assertIn("were not adjudicated", self.feedback[0]["bugs"])
        self.assertIn("Case n = 0 is missing.", self.feedback[0]["bugs"])
        self.assertEqual(result["output"], PROOF + " revised")

    def test_repair_names_the_candidate_file_by_absolute_path(self):
        critic = Critic(screens=[screen("fatal", "Lemma 2 fails for n = 1."), screen()], judges=[judge()])
        self.run_cheap(critic)
        path = Path(self.feedback[0]["candidate_file"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "saved-candidate.md")

    def test_changed_proof_labelled_unedited_is_reaudited(self):
        critic = Critic(screens=[screen()], judges=[judge(solution=PROOF + " silently changed"), judge()])
        result, _, _ = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge", "audit", "audit", "judge"])
        self.assertEqual(result["output"], PROOF + " silently changed")

    def test_verbatim_copy_counts_as_no_edit(self):
        critic = Critic(screens=[screen()], judges=[judge(solution=PROOF + "\n")])
        result, _, _ = self.run_cheap(critic)
        self.assertEqual(critic.kinds(), ["screen", "audit", "audit", "judge"])
        self.assertEqual(result["output"], PROOF)

    def test_judge_reject_keeps_the_candidate_and_sends_only_bugs(self):
        critic = Critic(screens=[screen(), screen()],
                        judges=[judge("reject", bugs="The bound in Lemma 3 is off by log n."), judge()])
        result, _, _ = self.run_cheap(critic)
        self.assertEqual(self.feedback[0]["bugs"], "The bound in Lemma 3 is off by log n.")
        self.assertEqual(self.feedback[0]["candidate_note"], "is your last final answer (read the file if that answer is no longer in your context)")
        self.assertEqual(result["output"], PROOF + " revised")

    def test_declared_edits_without_text_become_a_reject_not_an_approval(self):
        critic = Critic(screens=[screen(), screen()], judges=[judge(edits="substantive"), judge()],
                        audit_value=audit("issues", "Case n = 0 is missing."))
        result, _, _ = self.run_cheap(critic)
        self.assertIn("Case n = 0 is missing.", self.feedback[0]["bugs"])
        self.assertEqual(result["output"], PROOF + " revised")

    def test_critic_resume_rejection_points_a_new_author_to_the_saved_candidate(self):
        critic = Critic(screens=[screen("fatal", "Gap in step 4."), screen()], judges=[judge()])
        result, _, _ = self.run_cheap(critic, state={"statement": "Task", "solution": PROOF},
                                      options={"start_node": "critic"})
        instruction = self.author_kwargs["initial_instruction"]
        self.assertIn("Gap in step 4.", instruction)
        self.assertIn("is not in this conversation; read it before revising", instruction)
        self.assertIn("saved-candidate.md", instruction)
        self.assertEqual(result["output"], PROOF)


class CheapAuthorSessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        patcher = patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def thread_start(self, features, options):
        class Flattening(Runtime):
            codex_config_arguments = staticmethod(runtime.codex_config_arguments)
            merged_codex_config = staticmethod(runtime.merged_codex_config)
            _ultra_remapped = staticmethod(runtime._ultra_remapped)
        fake = Flattening([success()])
        session = runtime.goal_session(fake, "Exact assignment", prompts={
            "goal": "Goal.", "continuation": "Continue.", "compaction": "Compacted.",
            "repair": "{bugs}", "resume": "{original_prompt}"},
            settings={"model": "gpt-6-astra", "effort": "xhigh", "speed": "standard", "summary": "concise"},
            options={"goal_cwd": str(self.directory), **options}, features=features)
        self.addCleanup(session.close)
        self.assertEqual(next(session)["output"], "Full proof")
        config = next(p["config"] for m, p in fake.rpc.calls if m in {"thread/start", "thread/resume"})
        return self.spawn.call_args.args[0], config

    def test_cheap_options_reach_the_author_thread_on_start_and_resume(self):
        options = runtime.load_workflow(CHEAP)["options"]
        for extra in ({}, {"goal_thread_id": "thread-1"}):
            with self.subTest(extra=extra):
                command, config = self.thread_start(["multi_agent"], {**options, **extra})
                # Workflow overrides come first so the runner's own flags win.
                self.assertLess(command.index('personality="none"'), command.index("--enable"))
                # Codex replaces whole tables with the thread config, so the trims
                # must live inside it next to the runner's own keys.
                self.assertEqual(config["features"]["plugins"], False)
                self.assertEqual(config["features"]["apps"], False)
                self.assertEqual(config["features"]["view_image"], False)
                self.assertIs(config["features"]["goals"], True)
                self.assertIs(config["features"]["multi_agent"], True)
                self.assertEqual(config["tools"], {"update_plan": {"enabled": False}, "web_search": True})
                self.assertEqual(config["skills"], {"include_instructions": False})
                self.assertEqual(config["personality"], "none")
                self.assertEqual(config["agents"], {"max_concurrent_threads_per_session": 1})
                self.assertEqual(config["model_auto_compact_token_limit"], 150000)
                self.assertEqual(config["model_reasoning_effort"], "xhigh")

    def test_ultra_subagent_effort_is_remapped(self):
        options = {**runtime.load_workflow(CHEAP)["options"], "subagent_effort": "ultra"}
        _, config = self.thread_start(["multi_agent"], options)
        self.assertEqual(config["agents"]["default_subagent_reasoning_effort"], "xhigh")

    def test_goal_without_multi_agent_disables_collaboration_tools(self):
        command, config = self.thread_start([], {})
        self.assertEqual(command[command.index("--disable") + 1], "multi_agent")
        self.assertEqual(config["agents"], {"enabled": False})
        self.assertFalse(config["features"]["multi_agent"])
        self.assertIs(config["features"]["multi_agent_v2"], False)


class StructuredCommandTests(unittest.TestCase):
    def command(self, **kwargs):
        class Stop(Exception):
            pass
        recorded = []
        def popen(command, **_):
            recorded.append(command)
            raise Stop()
        with patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                patch.object(runtime, "codex", return_value="codex"):
            with self.assertRaises(Stop):
                runtime.run_structured_attempt("Prompt", {"type": "object"}, "critic", "gpt-6-astra",
                                               "xhigh", "standard", "concise", **kwargs)
        return recorded[0]

    def test_workflow_overrides_precede_runner_flags_and_subagents_stay_off(self):
        overrides = runtime.codex_config_arguments(runtime.load_workflow(CHEAP)["options"]["codex_config"])
        command = self.command(config_overrides=overrides)
        self.assertLess(command.index("skills.include_instructions=false"), command.index("-m"))
        self.assertLess(command.index("features.plugins=false"), command.index("exec"))
        self.assertIn("agents.enabled=false", command)
        self.assertIn("features.view_image=false", command)
        self.assertNotIn("agents.enabled=false", self.command(features=["multi_agent"]))

    def test_old_mocks_without_config_overrides_keep_working(self):
        def legacy(prompt, schema, stage, model, effort, speed, summary, timeout=None,
                   activity_label=None, features=()):
            return json.dumps({"verdict": "clean", "bugs": ""})
        schema = runtime.load_workflow(CHEAP)["nodes"]["critic"]["schema"]
        with patch.object(runtime, "run_structured_attempt", side_effect=legacy), patch.object(runtime, "emit"):
            report, _ = runtime.structured("Prompt", schema, "critic", attempts=1)
        self.assertEqual(report["verdict"], "clean")


if __name__ == "__main__":
    unittest.main()
