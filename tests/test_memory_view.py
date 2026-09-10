"""The optional browser viewer reads only the selected author's three files."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ui import server


class MemoryViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs = Path(temporary.name)
        self.original = self.runs / "original"
        self.resumed = self.runs / "resumed"
        self.original.mkdir()
        self.resumed.mkdir()
        self.app = server.App(runs=self.runs)
        self.app.run_dir = self.resumed
        self.app.state["goalWorkspace"] = str(self.original)

    def test_resumed_job_reads_original_files_without_modifying_them(self):
        for name in server.RESEARCH_MEMORY_FILES:
            path = self.original / name
            content = "# Saved work\n\n∀x: exact argument <script>text only</script>\n"
            path.write_text(content, encoding="utf-8")
            (self.resumed / name).write_text("Wrong workspace")
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            result = self.app.memory_file(name)
            self.assertEqual(result["content"], content)
            self.assertEqual(result["workspace"], "original")
            self.assertEqual(result["status"], "ready")
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_new_job_uses_its_own_workspace_and_missing_files_stay_missing(self):
        self.app.state["goalWorkspace"] = ""
        self.assertEqual(self.app.memory_file("APPROACHES.md")["status"], "missing")
        self.assertEqual(list(self.resumed.iterdir()), [])
        (self.resumed / "APPROACHES.md").write_text("New approach")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["content"], "New approach")
        self.app.state["goalWorkspace"] = str(self.original)
        self.assertEqual(self.app.memory_file("APPROACHES.md")["status"], "missing")

    def test_unchanged_poll_does_not_read_or_transfer_contents(self):
        path = self.original / "PROVED.md"
        path.write_text("# Lemmas")
        first = self.app.memory_file(path.name)
        with patch.object(Path, "read_text", side_effect=AssertionError("Unchanged file was read")):
            second = self.app.memory_file(path.name, first["version"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("content", second)
        path.write_text("# Lemmas\n\n## L001\nA new proved result.")
        updated = self.app.memory_file(path.name, first["version"])
        self.assertNotEqual(first["version"], updated["version"])
        self.assertIn("L001", updated["content"])

    def test_arbitrary_paths_and_symlinks_are_not_read(self):
        for name in ("../secret.txt", "job-settings.json", "/etc/passwd", "", "prompts/author.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.app.memory_file(name)
        secret = self.runs / "secret.txt"
        secret.write_text("Private contents")
        (self.original / "INITIAL_PROMPT.md").symlink_to(secret)
        result = self.app.memory_file("INITIAL_PROMPT.md")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("content", result)

    def test_temporary_unreadable_file_is_reported_without_hiding_other_files(self):
        (self.original / "PROVED.md").write_bytes(b"\xff")
        self.assertEqual(self.app.memory_file("PROVED.md")["status"], "unavailable")
        (self.original / "APPROACHES.md").write_text("Still readable")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["content"], "Still readable")

    def test_http_endpoint_requires_token_and_exact_job(self):
        (self.original / "PROVED.md").write_text("Lemma from the selected job")
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.jobs["resumed"] = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        url = http.origin + "/memory?job=resumed&file=PROVED.md"
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 403)
        request = Request(url, headers={"X-TCS-Prover-Token": http.token})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["content"], "Lemma from the selected job")
        for path in ("/memory?job=missing&file=PROVED.md", "/memory?job=resumed&file=secret.txt"):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(http.origin + path, headers={"X-TCS-Prover-Token": http.token}))
            self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
