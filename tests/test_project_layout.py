"""Check relocated entry points and assets from outside the project directory."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def run_python(self, arguments):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run(
                [sys.executable, *arguments], cwd=folder, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )

    def test_root_has_only_the_two_requested_python_entry_points(self):
        self.assertEqual(
            {path.name for path in ROOT.glob("*.py")},
            {"workflow_runner.py", "web_ui.py"},
        )

    def test_workflows_contains_only_the_two_yaml_definitions(self):
        self.assertEqual(
            {path.name for path in (ROOT / "workflows").iterdir()},
            {"author_critic.yaml", "clean_up.yaml"},
        )

    def test_command_line_help_works_outside_the_project(self):
        for relative, expected in (
            ("workflow_runner.py", "--critic-rounds"),
            ("web_ui.py", "--author-model"),
            ("transcript/view_transcript.py", "--root-only"),
        ):
            with self.subTest(entry_point=relative):
                result = self.run_python([str(ROOT / relative), "--help"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_package_imports_and_assets_are_independent_of_working_directory(self):
        # The standalone runner and the UI must import in either order.
        imports = (
            "import workflow_runner as runner\n"
            "from ui import cli, review, server\n",
            "from ui import server, review, cli\n"
            "import workflow_runner as runner\n",
        )
        for import_order in imports:
            with self.subTest(import_order=import_order):
                result = self.run_python(["-c", import_order + """
import json
from transcript import view_transcript
print(json.dumps({
    "project": str(runner.ROOT),
    "workflows": str(runner.WORKFLOWS),
    "ui": str(server.UI),
    "transcript_ui": str(view_transcript.UI_ROOT),
    "runs": str(server.RUNS),
    "transcript_project": str(view_transcript.ROOT),
    "author_prompt": runner.load_workflow(runner.WORKFLOWS / "author_critic.yaml")["prompts"]["author"],
    "cleanup_prompt": runner.load_workflow(runner.WORKFLOWS / "clean_up.yaml")["prompts"]["final"],
}))
"""])
                self.assertEqual(result.returncode, 0, result.stderr)
                values = json.loads(result.stdout)
                self.assertEqual(Path(values["project"]), ROOT)
                self.assertEqual(Path(values["transcript_project"]), ROOT)
                self.assertEqual(Path(values["workflows"]), ROOT / "workflows")
                self.assertEqual(Path(values["runs"]), ROOT / "runs")
                self.assertEqual(Path(values["ui"]), ROOT / "ui")
                self.assertEqual(
                    Path(values["transcript_ui"]),
                    ROOT / "transcript" / "transcript_ui",
                )
                self.assertIn("[STATEMENT]", values["author_prompt"])
                self.assertIn("LaTeX", values["cleanup_prompt"])
                for asset_root in (values["ui"], values["transcript_ui"]):
                    for filename in ("index.html", "app.js", "styles.css"):
                        self.assertTrue((Path(asset_root) / filename).is_file())


if __name__ == "__main__":
    unittest.main()
