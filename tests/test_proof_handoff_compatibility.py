"""Compatibility YAML expands before prompt or workflow execution handling."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime


class ProofHandoffCompatibilityTests(unittest.TestCase):
    def workflow(self):
        return {
            "prompts": {"critic": "Task: {statement}\nProof: {solution}"},
            "nodes": {"review": {
                "run": "structured", "prompt": "critic", "next": "end",
                "inputs": {"statement": "state.statement", "solution": "state.solution"},
                "response": {"solution": "string"},
                "proof_handoff": {"candidate": "solution", "statement": "statement"},
            }},
        }

    def load(self, value):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workflow.yaml"
            path.write_text(yaml.safe_dump(value), encoding="utf-8")
            return runtime.load_workflow(path)

    def compatibility(self, value):
        value = copy.deepcopy(value)
        node = value["nodes"]["review"]
        node["inputs"]["proof_handoff"] = repr(node.pop("proof_handoff"))
        return value

    def test_canonical_and_compatibility_yaml_normalize_to_the_same_graph(self):
        canonical = self.workflow()
        self.assertEqual(self.load(canonical), self.load(self.compatibility(canonical)))
        bundled = yaml.safe_load((runtime.WORKFLOWS / "author_critic.yaml").read_text())
        overlay = copy.deepcopy(bundled)
        node = overlay["nodes"]["critic"]
        node["inputs"]["proof_handoff"] = repr(node.pop("proof_handoff"))
        self.assertEqual(self.load(bundled), self.load(overlay))

    def test_normalized_controller_binding_is_not_a_prompt_input(self):
        loaded = self.load(self.compatibility(self.workflow()))
        node = loaded["nodes"]["review"]
        self.assertNotIn("proof_handoff", node["inputs"])
        context = {"state": {"statement": "exact statement", "solution": "exact proof"}, "visit": 1}
        self.assertEqual(runtime._request_prompt(node, loaded["prompts"], context),
                         "Task: exact statement\nProof: exact proof")
        loaded["prompts"]["critic"] = "{proof_handoff}"
        with patch.object(runtime, "require_model_credentials"):
            with self.assertRaisesRegex(ValueError, "Unknown prompt input"):
                runtime.prepare(loaded, {})

    def test_duplicate_definitions_are_rejected_even_when_equal(self):
        value = self.workflow()
        node = value["nodes"]["review"]
        node["inputs"]["proof_handoff"] = repr(node["proof_handoff"])
        with self.assertRaisesRegex(ValueError, "not both"):
            self.load(value)

    def test_nonconstant_malformed_or_ambiguous_bindings_are_rejected(self):
        invalid = [
            None, {}, 1, "", "state.handoff", "dict(candidate='solution', statement='statement')",
            "{'candidate': state.solution, 'statement': 'statement'}",
            "{'candidate': 'solution'}", "{'candidate': 'solution', 'statement': 'statement', 'extra': 'x'}",
            "{'candidate': 'first', 'candidate': 'solution', 'statement': 'statement'}",
            "{'candidate': 'same', 'statement': 'same'}",
            "{'candidate': 'not-a-field', 'statement': 'statement'}",
            "{'candidate': 7, 'statement': 'statement'}",
            "{'candidate': 'solution', 'statement': ['statement']}",
            "[('candidate', 'solution'), ('statement', 'statement')]",
            "{**{'candidate': 'solution'}, 'statement': 'statement'}",
        ]
        for expression in invalid:
            with self.subTest(expression=expression):
                value = self.compatibility(self.workflow())
                value["nodes"]["review"]["inputs"]["proof_handoff"] = expression
                with self.assertRaisesRegex(ValueError, "Proof handoff input"):
                    self.load(value)

    def test_compatibility_binding_does_not_expand_for_other_node_types(self):
        node = {"run": "goal", "inputs": {"proof_handoff":
            "{'candidate': 'solution', 'statement': 'statement'}"}}
        with self.assertRaisesRegex(ValueError, "Proof handoff input"):
            runtime._expand_node(node)


if __name__ == "__main__":
    unittest.main()
