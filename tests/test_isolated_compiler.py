"""Historical TeX compilation verifies and preserves the macOS boundary."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import isolated_compiler as compiler
import workflow_runner as runtime


CONFIG = {"argv": compiler.LATEX_ARGV, "requires": ["pdflatex"],
          "env": {"openin_any": "p", "openout_any": "p"}, "timeout": 120,
          "produces": ["final.pdf"], "log": "latex-compile.log", "result": "compilation"}
LATEX = r"\documentclass{article}\begin{document}Test.\end{document}"
HAS_MACTEX = sys.platform == "darwin" and Path("/Library/TeX/texbin/latexmk").exists()


class CompilerBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.policy = {"isolation": {"adapter": compiler.ADAPTER, "workspace": str(self.workspace)}}
        self.source = self.workspace / "latex-source.tex"
        self.source.write_text(LATEX, encoding="utf-8")
        self.output = self.workspace / "final.pdf"
        self.output.write_bytes(b"existing output")

    def compile(self, config=CONFIG):
        return compiler.compile_command(self.policy, config, config["argv"], self.workspace)

    def test_unsupported_commands_fail_before_tools_or_output_changes(self):
        for config in ({**CONFIG, "argv": ["sh", "-c", "true"]},
                       {**CONFIG, "env": {"TEXINPUTS": "/private"}},
                       {**CONFIG, "produces": ["../outside.pdf"]}):
            with self.subTest(config=config), patch.object(compiler.sys, "platform", "darwin"), patch.object(compiler, "toolchain") as tools:
                with self.assertRaises(compiler.CompilerIsolationError):
                    self.compile(config)
                tools.assert_not_called()
                self.assertEqual(self.output.read_bytes(), b"existing output")

    def test_workflow_command_uses_adapter(self):
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)
        with patch.object(runtime, "retrieval_policy", return_value=self.policy), patch.object(compiler, "compile_command", return_value={"status": "pass"}) as isolated:
            self.assertEqual(runtime._run_command(CONFIG, {"state": {}})["status"], "pass")
        isolated.assert_called_once_with(self.policy, CONFIG, compiler.LATEX_ARGV, self.workspace)

    def test_source_symlinks_and_hardlinks_are_rejected(self):
        target = self.workspace / "target.tex"
        target.write_text(LATEX)
        self.source.unlink()
        for link in (lambda: self.source.symlink_to(target), lambda: os.link(target, self.source)):
            link()
            with self.assertRaises(compiler.CompilerIsolationError):
                compiler._regular_file(self.source)
            self.source.unlink()

    def test_environment_drops_injected_credentials_and_tex_configuration(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "BRAVE_SEARCH_API_KEY": "secret",
                                    "TEXINPUTS": "/untrusted", "PERL5OPT": "-MUntrusted", "PATH": str(self.workspace)}):
            env = compiler.environment(self.workspace, Path("/trusted/tex/bin"))
        for key in ("OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "TEXINPUTS", "PERL5OPT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/trusted/tex/bin:/usr/bin:/bin")
        self.assertEqual(env["shell_escape"], "f")

    @unittest.skipUnless(HAS_MACTEX, "Requires macOS Seatbelt and MacTeX")
    def test_real_preflight_checks_sandbox_shell_escape_and_references(self):
        self.assertEqual(compiler.preflight(self.policy)["status"], "pass")
        self.assertEqual(self.output.read_bytes(), b"existing output")
        self.assertEqual(self.source.read_text(), LATEX)

    @unittest.skipUnless(HAS_MACTEX, "Requires macOS Seatbelt and MacTeX")
    def test_planting_executables_rc_files_and_shell_commands_does_not_escape(self):
        marker = self.workspace / "planted-tool-ran"
        for name in ("latexmk", "pdflatex"):
            path = self.workspace / name
            path.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
            path.chmod(0o700)
        (self.workspace / ".latexmkrc").write_text(f"open(my $f, '>', '{marker}'); die 'untrusted rc';")
        self.source.write_text(r"\documentclass{article}\begin{document}"
                               r"\ifnum\pdfshellescape=0\else\errmessage{Shell escape enabled}\fi "
                               r"\immediate\write18{touch shell-marker}"
                               r"\IfFileExists{shell-marker}{\errmessage{Shell command ran}}{}"
                               r"Safe.\end{document}")
        with patch.dict(os.environ, {"PATH": str(self.workspace), "TEXINPUTS": str(self.workspace), "PERL5OPT": "-MUntrusted"}):
            result = self.compile()
        self.assertEqual(result["status"], "pass", result["output"])
        self.assertFalse(marker.exists())
        self.assertTrue(self.output.read_bytes().startswith(b"%PDF-"))

    @unittest.skipUnless(HAS_MACTEX, "Requires macOS Seatbelt and MacTeX")
    def test_other_run_files_cannot_be_read_and_failed_pdf_is_removed(self):
        (self.workspace / "private-proof.tex").write_text("SECRET_PREVIOUS_PROOF")
        self.source.write_text(r"\documentclass{article}\begin{document}\input{private-proof.tex}\end{document}")
        result = self.compile()
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("SECRET_PREVIOUS_PROOF", result["output"])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_MACTEX, "Requires macOS Seatbelt and MacTeX")
    def test_broken_sandbox_is_rejected_without_deleting_existing_pdf(self):
        with patch.object(compiler, "sandbox_profile", return_value="(version 1) (allow default)"):
            with self.assertRaisesRegex(compiler.CompilerIsolationError, "canaries failed"):
                self.compile()
        self.assertEqual(self.output.read_bytes(), b"existing output")


if __name__ == "__main__":
    unittest.main()
