import copy
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_runner as runner


def node(prompt, field="text", **extra):
    return {
        "run": "structured", "prompt": prompt,
        "schema": {"type": "object", "properties": {field: {"type": "string"}}, "required": [field], "additionalProperties": False},
        "after": [{"set": {"output": f"result.{field}"}}],
        "next": {"done": "end"}, **extra,
    }


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)

    def run_document(self, document, state, replies, options=None, module=runner):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom-pipeline.yaml"
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(module.Path, "cwd", return_value=Path(directory)), patch.object(module, "structured", side_effect=[(value, json.dumps(value)) for value in replies]) as calls, patch.object(module, "emit"):
                result = module.execute(path, state, options=options)
            return result, calls.call_args_list

    def test_unrelated_node_names_schemas_state_and_instructions_work(self):
        document = {
            "prompts": {"extract": "Collect facts from {source}.", "format": "{instructions}\n{facts}", "style": r"Use LaTeX \\frac{a}{b} literally."},
            "nodes": {
                "extract_facts": node("extract", "facts", role="extractor", stage="collect", inputs={"source": "state.input"}, after=[{"set": {"facts": "result.facts"}}], next={"done": "format_facts"}),
                "format_facts": node("format", "html", role="formatter", stage="format", instructions=["style"], inputs={"facts": "state.facts"}, features=["multi_agent"], activity_label="Formatting facts"),
            },
        }
        result, calls = self.run_document(document, {"input": "SOURCE Ω"}, [{"facts": "FACTS λ"}, {"html": "FORMATTED"}], {"extractor_model": "gpt-5.6-luna"})
        self.assertEqual(result["output"], "FORMATTED")
        self.assertEqual(calls[0].args[0], "Collect facts from SOURCE Ω.")
        self.assertEqual(calls[1].args[0], r"Use LaTeX \\frac{a}{b} literally." + "\nFACTS λ")
        self.assertEqual(calls[0].kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(calls[1].kwargs["features"], ["multi_agent"])
        self.assertEqual(calls[1].kwargs["activity_label"], "Formatting facts")
        self.assertNotIn("request_label", calls[1].kwargs)
        self.assertEqual([call.args[2] for call in calls], ["collect", "format"])

    def test_repeat_rule_and_override_are_independent_of_roles_and_reset_on_exit(self):
        document = {
            "prompts": {"draft": "Write.", "inspect": "Inspect {body}; visit {visit}."},
            "nodes": {
                "create": node("draft", after=[{"set": {"body": "result.text"}}], next={"done": "inspect"}),
                "inspect": node("inspect", inputs={"body": "state.body", "visit": "visit"}, outcome="result.decision", after=[{"set": {"body": "result.text"}}], next={
                    "retry": "create", "finish": "end", "adjust": {"repeat": 5, "option": "inspection_limit", "then": "end", "after": [{"set": {"output": "state.body"}}]},
                }),
            },
        }
        replies = [{"text": "first"}, {"text": "fixed one", "decision": "adjust"}, {"text": "rejected", "decision": "retry"}, {"text": "replacement"}, {"text": "fixed two", "decision": "adjust"}, {"text": "accepted", "decision": "adjust"}]
        result, calls = self.run_document(document, {}, replies, {"inspection_limit": 2})
        self.assertEqual(result["output"], "accepted")
        self.assertEqual([call.args[0] for call in calls], ["Write.", "Inspect first; visit 1.", "Inspect fixed one; visit 2.", "Write.", "Inspect replacement; visit 1.", "Inspect fixed two; visit 2."])

    def test_custom_runner_does_not_need_the_bundled_workflow_files(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "workflow_runner.py"
            shutil.copyfile(runner.__file__, destination)
            spec = importlib.util.spec_from_file_location("isolated_runner", destination)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertFalse(module.WORKFLOWS.exists())
            document = {"prompts": {"custom": "Handle {source}"}, "nodes": {"unrelated": node("custom", inputs={"source": "state.input"})}}
            result, _ = self.run_document(document, {"input": "QUESTION"}, [{"text": "ANSWER"}], module=module)
            self.assertEqual(result["output"], "ANSWER")

    def test_conditional_prompt_selection_and_early_exit(self):
        document = {"prompts": {"short": "Short.", "long": "Long."}, "nodes": {"choose": node({"when": "state.brief", "then": "short", "else": "long"}, outcome="result.status", next={"good": "end", "retry": {"repeat": 3, "then": "end"}})}}
        result, calls = self.run_document(document, {"brief": True}, [{"text": "accepted", "status": "good"}])
        self.assertEqual(result["output"], "accepted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], "Short.")

    def test_goal_keeps_literal_prompt_and_closes_when_later_node_fails(self):
        workflow = copy.deepcopy(runner.builtin_workflow("author_critic"))
        workflow["prompts"]["author"] = "Literal {LaTeX} and [STATEMENT]\n"
        closed = []
        captured = []

        def session(prompt, *args, **kwargs):
            captured.append(prompt)
            try:
                yield {"outcome": "proof", "solution": "CANDIDATE"}
            finally:
                closed.append(True)

        with patch.object(runner, "author_session", side_effect=session), self.assertRaises(runner.Error):
            self.run_document(workflow, {"statement": "THEOREM"}, [{"verdict": "pass"}])
        self.assertEqual(captured, ["Literal {LaTeX} and THEOREM\n"])
        self.assertEqual(closed, [True])

    def test_entire_chain_is_validated_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            valid, invalid = Path(directory) / "valid.yaml", Path(directory) / "invalid.yaml"
            valid.write_text(json.dumps({"prompts": {"work": "Work"}, "nodes": {"start": node("work")}}))
            invalid.write_text(json.dumps({"prompts": {}, "nodes": {}}))
            with patch.object(runner, "structured") as calls, self.assertRaises(ValueError):
                runner.execute_workflows([valid, invalid], {})
            calls.assert_not_called()

    def test_cli_emits_output_for_a_custom_workflow_with_default_done_outcome(self):
        document = {
            "prompts": {"work": "Work on {source}."},
            "nodes": {"custom": node("work", inputs={"source": "state.input"})},
        }
        response = {"text": "CUSTOM OUTPUT Ω"}
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(json.dumps(document), encoding="utf-8")
            with (
                patch.object(runner.sys, "stdin", io.StringIO("CUSTOM INPUT")),
                patch.object(runner.sys, "stdout", output),
                patch.object(runner.sys, "stderr", errors),
                patch.object(
                    runner, "structured",
                    return_value=(response, json.dumps(response)),
                ) as calls,
            ):
                status = runner.main([str(path)])

        self.assertEqual(status, 0, errors.getvalue())
        calls.assert_called_once()
        self.assertEqual(calls.call_args.args[0], "Work on CUSTOM INPUT.")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        results = [event for event in events if event["kind"] == "workflow_result"]
        self.assertEqual(results, [{
            "kind": "workflow_result", "stage": "workflow", "output": "CUSTOM OUTPUT Ω",
        }])

    def test_cleanup_does_not_run_after_author_failure(self):
        def session(*args, **kwargs):
            yield {"outcome": "failure", "output": "Unsolved summary"}
        with patch.object(runner, "author_session", side_effect=session), patch.object(runner, "structured") as calls:
            state = runner.execute_workflows([runner.WORKFLOWS / "author_critic.yaml", runner.WORKFLOWS / "clean_up.yaml"], {"statement": "Theorem"})
        self.assertTrue(state["failed"])
        self.assertEqual(state["output"], "Unsolved summary")
        calls.assert_not_called()


class WorkflowValidationTests(unittest.TestCase):
    def load(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(json.dumps(document))
            return runner.load_workflow(path)

    def test_rejects_unknown_targets_and_missing_branches(self):
        for branches in ({}, {"done": "missing"}):
            with self.subTest(branches=branches), self.assertRaises(ValueError):
                self.load({"prompts": {"work": "Work"}, "nodes": {"start": node("work", next=branches)}})

    def test_builtin_helpers_reject_empty_inputs_before_a_model_call(self):
        cases = (
            (runner.criticize, ("", "Proof", 1)),
            (runner.criticize, ("Theorem", " \n\t", 1)),
            (runner.finalize, (" \n\t", "Proof")),
            (runner.finalize, ("Theorem", "")),
            (runner.polish, (" \n\t",)),
        )
        for operation, arguments in cases:
            with self.subTest(operation=operation.__name__, arguments=arguments):
                with patch.object(runner, "structured") as calls:
                    with self.assertRaises(runner.Error):
                        operation(*arguments)
                    calls.assert_not_called()

    def test_rejects_invalid_json_schemas_when_loading_the_workflow(self):
        for schema in (
            {"type": "unknown-type"},
            {"type": "object", "properties": {"text": {"type": 7}}},
            {"type": "object", "required": "text"},
        ):
            with self.subTest(schema=schema), self.assertRaisesRegex(
                ValueError, "Invalid JSON schema",
            ):
                self.load({
                    "prompts": {"work": "Work"},
                    "nodes": {"start": node("work", schema=schema)},
                })

    def test_raw_schema_validation_enforces_numeric_and_null_types_and_bounds(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 2},
                "score": {"type": "number", "exclusiveMaximum": 10},
                "optional": {"type": "null"},
            },
            "required": ["count", "score", "optional"], "additionalProperties": False,
        }
        valid = {"count": 2, "score": 2.5, "optional": None}
        self.assertIs(runner.validate_json_schema(valid, schema), valid)
        for changes in (
            {"count": True}, {"count": 1}, {"score": "2.5"},
            {"score": True}, {"score": 10}, {"optional": "null"},
        ):
            with self.subTest(changes=changes), self.assertRaises(runner.Error):
                runner.validate_json_schema({**valid, **changes}, schema)

    def test_rejects_invalid_repeat_limits(self):
        for value in (0, -1, 1.5, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"prompts": {"work": "Work"}, "nodes": {"start": node("work", next={"done": {"repeat": value, "then": "end"}})}})

    def test_rejects_executable_yaml_and_expressions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.yaml"
            path.write_text("!!python/object/apply:builtins.str [123]\n")
            with self.assertRaises(ValueError):
                runner.load_workflow(path)
        for expression in ("__import__('os')", "state.clear()", "(lambda: 1)()", "open('secret')"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.load({"prompts": {"work": "Work"}, "nodes": {"start": node("work", outcome=expression)}})

    def test_later_structured_templates_are_validated_before_any_model_call(self):
        for template in ("Inspect {source", "Inspect {unbound}"):
            for override in (False, True):
                with self.subTest(template=template, override=override):
                    document = {
                        "prompts": {"first": "Create text.", "later": "Inspect {source}."},
                        "nodes": {
                            "first": node("first", next={"done": "later"}),
                            "later": node("later", inputs={"source": "state.output"}),
                        },
                    }
                    options = {}
                    if override:
                        options["prompts"] = {"later": template}
                    else:
                        document["prompts"]["later"] = template
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "later-template.yaml"
                        path.write_text(json.dumps(document), encoding="utf-8")
                        with patch.object(
                            runner, "structured", return_value=({"text": "DRAFT"}, "{}"),
                        ) as calls:
                            with self.assertRaises(ValueError):
                                runner.execute(path, {}, options=options)
                            calls.assert_not_called()

    def test_goal_resumption_requires_candidate_feedback_and_round_bindings(self):
        for missing in ("solution", "bugs", "round"):
            with self.subTest(missing=missing):
                document = copy.deepcopy(runner.builtin_workflow("author_critic"))
                del document["nodes"]["author"]["resume"][missing]
                with self.assertRaisesRegex(ValueError, "resumption"):
                    self.load(document)

    def test_goal_stages_require_nonempty_strings(self):
        for stage in ("initial", "resume", "failure"):
            for value in ("", None, 7):
                with self.subTest(stage=stage, value=value):
                    document = copy.deepcopy(runner.builtin_workflow("author_critic"))
                    document["nodes"]["author"]["stages"][stage] = value
                    with self.assertRaisesRegex(ValueError, "Invalid goal stages"):
                        self.load(document)

    def test_bad_override_is_rejected_before_model_call(self):
        document = {"prompts": {"work": "Work"}, "nodes": {"start": node("work", next={"done": {"repeat": 2, "option": "attempts", "then": "end"}})}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(json.dumps(document))
            with patch.object(runner, "structured") as calls, self.assertRaises(ValueError):
                runner.execute(path, {}, options={"attempts": 1.9})
            calls.assert_not_called()

    def test_expressions_short_circuit_and_templates_do_not_recurse(self):
        self.assertTrue(runner.evaluate("not state.enabled or state.missing", {"state": {"enabled": False}}))
        self.assertTrue(runner.evaluate("all(item.ok for item in result)", {"result": [{"ok": True}]}))
        self.assertEqual(runner.render_template("{instructions}\n{source}", {"instructions": r"\\frac{a}{b}", "source": "{value}"}), r"\\frac{a}{b}" + "\n{value}")
        with self.assertRaises(ValueError):
            runner.evaluate("state.input.__class__", {"state": {"input": "text"}})


if __name__ == "__main__":
    unittest.main()
