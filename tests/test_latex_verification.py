from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from latex_verification import verify_latex


class LatexVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = r"\documentclass{article}\begin{document}A complete argument.\end{document}"

    def test_unavailable_is_explicit_and_preserves_exact_source(self):
        with patch("latex_verification.shutil.which", return_value=None):
            report = verify_latex(self.source, self.directory)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["engine"])
        self.assertIn("not been compiled", report["diagnostic"])
        self.assertEqual((self.directory / "final-validation/main.tex").read_text(), self.source)
        self.assertIn("unavailable", (self.directory / "final-validation/compile.log").read_text())

    def test_success_requires_pdf_and_uses_restricted_bounded_invocation(self):
        def compile_fake(command, **kwargs):
            self.assertEqual(command[0], "/safe/pdflatex")
            self.assertIn("-no-shell-escape", command)
            self.assertIn("-interaction=nonstopmode", command)
            self.assertIn("-halt-on-error", command)
            self.assertEqual(command[-1], "main.tex")
            self.assertEqual(kwargs["env"]["openout_any"], "p")
            self.assertEqual(kwargs["env"]["openin_any"], "p")
            self.assertEqual(kwargs["env"]["shell_escape"], "f")
            self.assertEqual(kwargs["timeout"], 7)
            self.assertEqual(kwargs["umask"], 0o077)
            self.assertNotIn("shell", kwargs)
            (Path(kwargs["cwd"]) / "main.pdf").write_bytes(b"%PDF-1.4 test")
            return subprocess.CompletedProcess(command, 0, "Successful compiler output ∀x")
        with patch("latex_verification.shutil.which", return_value="/safe/pdflatex"), \
                patch("latex_verification.subprocess.run", side_effect=compile_fake):
            report = verify_latex(self.source, self.directory, timeout=7)
        self.assertEqual(report["status"], "pass")
        self.assertIn("Successful compiler output ∀x", (self.directory / "final-validation/compile.log").read_text())
        self.assertEqual((self.directory / "final-validation/main.tex").stat().st_mode & 0o777, 0o600)

    def test_invalid_document_retains_full_compiler_log(self):
        output = "Errors and detailed context.\n" * 10000
        with patch("latex_verification.shutil.which", return_value="/safe/pdflatex"), \
                patch("latex_verification.subprocess.run", return_value=subprocess.CompletedProcess([], 1, output)):
            report = verify_latex(self.source, self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertIn("exit code 1", report["diagnostic"])
        self.assertIn(output, (self.directory / "final-validation/compile.log").read_text())

    def test_timeout_retains_partial_output(self):
        error = subprocess.TimeoutExpired("pdflatex", 3, output=b"partial log", stderr=b"additional detail")
        with patch("latex_verification.shutil.which", return_value="/safe/pdflatex"), \
                patch("latex_verification.subprocess.run", side_effect=error):
            report = verify_latex(self.source, self.directory, timeout=3)
        self.assertEqual(report["status"], "fail")
        self.assertIn("3 seconds", report["diagnostic"])
        log = (self.directory / "final-validation/compile.log").read_text()
        self.assertIn("partial log", log)
        self.assertIn("additional detail", log)

    def test_stale_pdf_cannot_produce_false_success(self):
        (self.directory / "final-validation").mkdir()
        (self.directory / "final-validation/main.pdf").write_bytes(b"previous valid PDF")
        with patch("latex_verification.shutil.which", return_value="/safe/pdflatex"), \
                patch("latex_verification.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "No PDF")):
            report = verify_latex(self.source, self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertIn("no nonempty PDF", report["diagnostic"])

    def test_compiler_launch_error_is_visible(self):
        with patch("latex_verification.shutil.which", return_value="/safe/pdflatex"), \
                patch("latex_verification.subprocess.run", side_effect=PermissionError("cannot execute")):
            report = verify_latex(self.source, self.directory)
        self.assertEqual(report["status"], "fail")
        self.assertIn("cannot execute", report["diagnostic"])

    def test_storage_errors_are_not_reported_as_compiler_unavailable(self):
        with patch("latex_verification._atomic_text", side_effect=PermissionError("disk full")):
            with self.assertRaisesRegex(PermissionError, "disk full"):
                verify_latex(self.source, self.directory)

    def test_timeout_must_be_a_positive_finite_bound(self):
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                verify_latex(self.source, self.directory, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
