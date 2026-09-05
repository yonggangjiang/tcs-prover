import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from transcript import view_transcript


ROOT = Path(__file__).resolve().parent.parent


def record(kind="status", stage="solve", text="Searching λ", **extra):
    return {
        "kind": kind, "stage": stage, "text": text,
        "time": "2026-08-03T16:20:24+00:00", **extra,
    }


def encoded(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class TranscriptRelocationTests(unittest.TestCase):
    def test_default_run_and_asset_roots_are_preserved(self):
        self.assertEqual(view_transcript.ROOT, ROOT)
        self.assertEqual(view_transcript.UI_ROOT, ROOT / "transcript/transcript_ui")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "runs/older/transcript.jsonl"
            new = root / "runs/newer/transcript.jsonl"
            for path, modified in ((old, 100), (new, 200)):
                path.parent.mkdir(parents=True)
                path.write_bytes(encoded(record()))
                os.utime(path, (modified, modified))
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(elsewhere)
                with patch.object(view_transcript, "ROOT", root):
                    self.assertEqual(view_transcript.transcript_path(), new.resolve())
                    self.assertEqual(view_transcript.transcript_path(old.parent), old.resolve())
            finally:
                os.chdir(original_cwd)

    def test_script_and_module_render_identical_terminal_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transcript.jsonl"
            path.write_bytes(
                encoded(record("request", text="Exact mathematical prompt λ", model="gpt-6-astra"))
                + encoded(record("author_result", text="A proof with λ."))
                + b"malformed JSON\n"
            )
            script = subprocess.run(
                [sys.executable, str(ROOT / "transcript/view_transcript.py"), str(path), "--text"],
                cwd=folder, capture_output=True, text=True, timeout=10,
            )
            module = subprocess.run(
                [sys.executable, "-m", "transcript.view_transcript", str(path), "--text"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(script.returncode, 0, script.stderr)
            self.assertEqual(module.returncode, 0, module.stderr)
            self.assertEqual(script.stdout, module.stdout)
            self.assertIn("PROOF AUTHOR", script.stdout)
            self.assertIn("gpt-6-astra", script.stdout)
            self.assertIn("A proof with λ.", script.stdout)
            self.assertNotIn("Exact mathematical prompt λ", script.stdout)
            self.assertIn("Read 3 records; displayed 3 entries; malformed 1.", script.stdout)

    def test_output_file_and_filters_keep_the_same_narrative(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "transcript.jsonl"
            destination = Path(folder) / "nested/readable.txt"
            source.write_bytes(
                encoded(record("request", text="Author prompt"))
                + encoded(record("request", stage="critic", text="Critic prompt"))
                + encoded(record("status", stage="critic", text="Checking proof"))
            )
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                code = view_transcript.main([
                    str(source), "--output", str(destination), "--prompts", "--stage", "critic",
                ])
            self.assertEqual(code, 0)
            self.assertIn("Wrote readable transcript", output.getvalue())
            narrative = destination.read_text(encoding="utf-8")
            self.assertIn("INDEPENDENT CRITIC", narrative)
            self.assertIn("Critic prompt", narrative)
            self.assertIn("Checking proof", narrative)
            self.assertNotIn("Author prompt", narrative)
            self.assertIn("Read 3 records; displayed 2 entries", narrative)


class TranscriptStreamingTests(unittest.TestCase):
    def test_partial_live_line_is_retried_after_writer_finishes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transcript.jsonl"
            first = encoded(record(text="First λ"))
            second = encoded(record(text="Second λ"))
            path.write_bytes(first + second[:-4])
            initial = view_transcript.read_ui_batch(path)
            self.assertEqual([entry["text"] for entry in initial["entries"]], ["First λ"])
            self.assertEqual(initial["nextOffset"], len(first))
            self.assertFalse(initial["eof"])
            with path.open("ab") as destination:
                destination.write(second[-4:])
            resumed = view_transcript.read_ui_batch(path, initial["nextOffset"])
            self.assertEqual([entry["text"] for entry in resumed["entries"]], ["Second λ"])
            self.assertEqual(resumed["entries"][0]["id"], str(len(first)))
            self.assertTrue(resumed["eof"])

    def test_cursor_skips_protocol_noise_and_retains_malformed_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transcript.jsonl"
            noise = encoded(record("codex_event", event={"method": "thread/started"}))
            visible = encoded(record())
            path.write_bytes(noise + visible + b"bad JSON\n" + encoded(record(text="Last")))
            initial = view_transcript.read_ui_batch(path, limit=1)
            self.assertEqual(len(initial["entries"]), 1)
            self.assertEqual(initial["entries"][0]["id"], str(len(noise)))
            resumed = view_transcript.read_ui_batch(path, initial["nextOffset"])
            self.assertEqual([entry["category"] for entry in resumed["entries"]], ["diagnostic", "status"])
            self.assertEqual(resumed["malformed"], 1)
            self.assertEqual(resumed["entries"][1]["text"], "Last")
            self.assertTrue(resumed["eof"])
            self.assertEqual(view_transcript.read_ui_batch(path, 10 ** 6, limit=1), initial)


class TranscriptHTTPTests(unittest.TestCase):
    def test_relocated_assets_and_authenticated_incremental_api(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transcript.jsonl"
            first = encoded(record())
            path.write_bytes(first + encoded(record("final_result", stage="final", output="\\begin{proof} λ \\end{proof}")))
            server = view_transcript.TranscriptHTTPServer(("127.0.0.1", 0), path, "test-token")
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()

            def get(route, authenticated=False):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    headers = {"X-Transcript-Token": "test-token"} if authenticated else {}
                    connection.request("GET", route, headers=headers)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            try:
                for route, filename in (("/", "index.html"), ("/app.js", "app.js"), ("/styles.css", "styles.css")):
                    with self.subTest(route=route):
                        status, headers, body = get(route)
                        self.assertEqual(status, 200)
                        self.assertEqual(body, (view_transcript.UI_ROOT / filename).read_bytes())
                        self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(get("/api/meta")[0], 403)
                status, _, body = get("/api/meta", authenticated=True)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["path"], str(path))
                status, _, body = get("/api/events?limit=1", authenticated=True)
                self.assertEqual(status, 200)
                batch = json.loads(body)
                self.assertEqual(batch["nextOffset"], len(first))
                status, _, body = get(f"/api/events?offset={batch['nextOffset']}", authenticated=True)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["entries"][0]["resultKind"], "final_result")
                self.assertEqual(get("/api/events?offset=invalid", authenticated=True)[0], 400)
                self.assertEqual(get("/../README.md")[0], 404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
