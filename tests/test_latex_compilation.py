"""Compile-and-repair behavior, including real TeX runs without paid model calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime


LATEX = r"\documentclass{article}\begin{document}\section{Proof}\label{proof}See Section~\ref{proof}.\end{document}"
BROKEN = LATEX.replace("See Section", r"\undefinedcommand See Section")


class LatexCompilationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.events = []

    def execute(self, model, **options):
        with patch.object(runtime, "structured", side_effect=model), patch.object(
            runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append((kind, fields))
        ):
            return runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": BROKEN}, options)

    def test_errors_feed_back_until_compilation_succeeds(self):
        candidates = [BROKEN, BROKEN + "\n% first repair", LATEX]
        calls, builds = [], []

        def model(prompt, schema, stage, **settings):
            calls.append(prompt)
            if len(calls) > 1:
                self.assertIn(candidates[len(calls) - 2], prompt)
                self.assertIn("Undefined control sequence", prompt)
                self.assertIn("smallest necessary", prompt)
                self.assertEqual(settings["model"], "gpt-5.6-sol")
                self.assertFalse((self.directory / "final.tex").exists())
                self.assertFalse(any(kind == "final_result" for kind, _ in self.events))
            value = {"latex": candidates[len(calls) - 1]}
            return value, json.dumps(value)

        def command(argv, **kwargs):
            builds.append((self.directory / "latex-source.tex").read_text())
            # Even a failed engine can leave a partial PDF; it must be discarded.
            (self.directory / "final.pdf").write_bytes(b"%PDF-1.4\nPDF bytes\n%%EOF")
            return subprocess.CompletedProcess(argv, 0 if len(builds) == 3 else 1,
                                               "Built" if len(builds) == 3 else "Undefined control sequence")

        with patch.object(runtime.shutil, "which", return_value="/tools/compiler"), patch.object(runtime.subprocess, "run", side_effect=command):
            state = self.execute(model, writer_model="gpt-5.6-sol", writer_effort="high")
        self.assertEqual(builds, candidates)
        self.assertEqual(state["output"], LATEX)
        self.assertEqual((self.directory / "final.tex").read_text(), LATEX)
        self.assertTrue((self.directory / "final.pdf").is_file())
        self.assertEqual([kind for kind, _ in self.events].count("final_result"), 1)

    def test_missing_tool_preserves_source_without_repair_requests(self):
        for missing in ("latexmk", "pdflatex"):
            with self.subTest(missing=missing), patch.object(runtime.shutil, "which", side_effect=lambda name: None if name == missing else "/tools/" + name):
                calls = []

                def model(*args, **kwargs):
                    calls.append(1)
                    return {"latex": LATEX}, ""

                with self.assertRaisesRegex(runtime.Error, "Install latexmk"):
                    self.execute(model)
                self.assertEqual(len(calls), 1)
                self.assertEqual((self.directory / "latex-source.tex").read_text(), LATEX)
                self.assertFalse((self.directory / "final.tex").exists())
                self.assertFalse((self.directory / "final.pdf").exists())
                self.assertFalse(any(kind == "final_result" for kind, _ in self.events))

    def test_failed_compile_removes_partial_pdf_and_obeys_workflow_budget(self):
        remaining = [60]

        def command(argv, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 60)
            (self.directory / "final.pdf").write_bytes(b"partial PDF")
            remaining[0] = 0
            return subprocess.CompletedProcess(argv, 1, "Compilation error")

        with patch.object(runtime.shutil, "which", return_value="/tools/compiler"), patch.object(runtime.subprocess, "run", side_effect=command), patch.object(runtime, "workflow_remaining", side_effect=lambda options: remaining[0]):
            with self.assertRaisesRegex(runtime.Error, "time limit"):
                self.execute(lambda *args, **kwargs: ({"latex": BROKEN}, ""))
        self.assertFalse((self.directory / "final.pdf").exists())
        self.assertFalse((self.directory / "final.tex").exists())
        self.assertIn("Compilation error", (self.directory / "latex-compile.log").read_text())

    def test_command_node_runs_without_a_prompt_or_model_credentials(self):
        workflow = {"prompts": {}, "nodes": {"build": {
            "run": "command", "command": {"argv": ["builder"], "timeout": 1, "result": "build"},
            "outcome": "result.status", "next": {"pass": "end"},
        }}}
        import yaml
        path = self.directory / "workflow.yaml"
        path.write_text(yaml.safe_dump(workflow))
        with patch.object(runtime, "require_model_credentials", side_effect=AssertionError("Model started")), patch.object(runtime, "_run_command", return_value={"status": "pass", "output": "built"}), contextlib.redirect_stdout(io.StringIO()):
            state = runtime.execute(path)
        self.assertEqual(state["build"]["status"], "pass")
        invalid = copy.deepcopy(workflow)
        invalid["nodes"]["build"]["command"]["timeout"] = 0
        path.write_text(yaml.safe_dump(invalid))
        with self.assertRaisesRegex(ValueError, "timeout"):
            runtime.load_workflow(path)

    @unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "TeX is not installed")
    def test_real_latex_failure_then_repair_produces_pdf_and_stable_references(self):
        calls = []

        def model(prompt, *args, **kwargs):
            calls.append(prompt)
            self.assertLessEqual(len(calls), 2, prompt[-2000:])
            if len(calls) == 2:
                self.assertIn("Undefined control sequence", prompt)
                self.assertFalse((self.directory / "final.pdf").exists())
            return {"latex": BROKEN if len(calls) == 1 else LATEX}, ""

        self.execute(model)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.directory / "final.tex").read_text(), LATEX)
        self.assertTrue((self.directory / "final.pdf").read_bytes().startswith(b"%PDF-"))
        log = (self.directory / "final.log").read_text()
        self.assertNotIn("undefined references", log)
        self.assertNotIn("Rerun to get cross-references right", log)


if __name__ == "__main__":
    unittest.main()
