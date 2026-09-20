"""The cheaper service tier keeps model strength and explicit Fast overrides."""

import unittest

import workflow_runner as runtime
from ui import server


class EfficiencyDefaultTests(unittest.TestCase):
    def test_new_workflow_uses_standard_tier_with_astra_ultra(self):
        workflow = runtime.builtin_workflow("author_critic")
        for role in ("author", "critic"):
            settings = runtime._settings(workflow["nodes"][role], {})
            self.assertEqual(settings["model"], "gpt-6-astra")
            self.assertEqual(settings["effort"], "ultra")
            self.assertEqual(settings["speed"], "standard")
            self.assertEqual(runtime.speed_arguments(settings["speed"], settings["model"]),
                             ["--disable", "fast_mode"])
        self.assertEqual(server.empty_state()["speedMode"], "standard")

    def test_explicit_fast_selection_remains_available(self):
        workflow = runtime.builtin_workflow("author_critic")
        settings = runtime._settings(workflow["nodes"]["author"], {"speed": "fast"})
        self.assertEqual(settings["model"], "gpt-6-astra")
        self.assertEqual(settings["effort"], "ultra")
        self.assertEqual(settings["speed"], "fast")
        self.assertIn('service_tier="fast"', runtime.speed_arguments(settings["speed"], settings["model"]))


if __name__ == "__main__":
    unittest.main()
