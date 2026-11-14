"""The server's side of the job records (plan v3 section 1): a record for every live job's runners, snapshots when
a runner ends and before a job is deleted, records that outlive their job, and the record routes."""

import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers, records
from ui import server

import test_records as record_test


class AppCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        for change in (mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")}),
                       mock.patch.object(server, "grant_windows_access")):
            change.start()
            self.addCleanup(change.stop)

    def new_app(self, statement="Exact statement\nsecond line"):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run(statement)
        app.state["runId"] = app.run_dir.name
        return app

    def record(self, app):
        return records.Record(records.record_folder(app.run_dir.name))


class RunnerEnvironmentTests(AppCase):
    def test_a_live_job_s_runners_get_its_record(self):
        app = self.new_app()
        environment = app._retrieval_child_environment()
        folder = records.record_folder(app.run_dir.name)
        self.assertEqual(environment[records.RECORD_ENV], str(folder))
        self.assertEqual(folder.parent, providers.state_directory() / "records")
        manifest = self.record(app).manifest()
        self.assertEqual((manifest["runFolder"], manifest["title"], manifest["webCutoff"]),
                         (app.run_dir.name, "Exact statement", False))

    def test_a_web_cutoff_job_s_runners_never_do(self):
        app = self.new_app()
        app.state["retrievalMode"] = "historical"
        with mock.patch.dict(os.environ, {records.RECORD_ENV: "/inherited/record"}):
            environment = app._retrieval_child_environment()
        self.assertNotIn(records.RECORD_ENV, environment)
        self.assertTrue(self.record(app).manifest()["webCutoff"])


class SnapshotTests(AppCase):
    def running_app(self):
        app = self.new_app()
        app.state.update(phase="running", stage="solve", activeNode="author")
        app._save_controlled("prompts/author.txt", "Prove it.")
        app._save_job_settings(app.state)
        app.add_trace({"kind": "status", "stage": "solve", "label": "Goal started", "text": "Working."})
        process = mock.Mock(stdout=io.StringIO(json.dumps({"kind": "workflow_result", "stage": "workflow",
                                                           "output": "Done."}) + "\n"))
        process.wait.return_value = process.poll.return_value = 0
        token = object()
        app.process, app.worker_token, app.active_token = process, token, token
        return app, process, token

    def test_a_runner_that_ends_leaves_a_snapshot_in_the_record(self):
        app, process, token = self.running_app()
        app._read_output(process, token)
        transcript = records.read_transcript(self.record(app))
        self.assertIn("Goal started", [entry.get("label") for entry in transcript])
        folder = self.record(app).folder / "transcript"
        self.assertTrue((folder / "job-settings.json").is_file())
        self.assertEqual((folder / "prompts" / "author.txt").read_text(), "Prove it.")

    def test_a_record_outlives_its_deleted_and_trashed_job(self):
        app, process, token = self.running_app()
        app._read_output(process, token)
        app.add_trace({"kind": "status", "stage": "solve", "label": "Last words", "text": "After the runner."})
        http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(http.server_close)
        http.jobs[app.run_dir.name] = app
        name = app.run_dir.name
        http.delete_job(name)
        self.assertFalse((self.runs / name).exists())
        # The record kept everything the job had when it was deleted, and the trash's pruning leaves it.
        record = records.Record(records.record_folder(name))
        self.assertIn("Last words", [entry.get("label") for entry in records.read_transcript(record)])
        server.prune_trash(self.runs, now=time.time() + 365 * 86400)
        self.assertEqual(list((self.runs / server.TRASH_FOLDER).glob(f"{name}*")), [])
        self.assertTrue(record.folder.is_dir())
        self.assertEqual([row["jobExists"] for row in server.model_calls.record_list(lambda run: run in http.jobs)],
                         [False])


class RouteTests(AppCase):
    def setUp(self):
        super().setUp()
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)
        self.app = self.new_app()
        self.http.jobs[self.app.run_dir.name] = self.app
        self.name = self.app.run_dir.name
        record = records.Record.create(records.record_folder(self.name), title="Exact statement")
        trace = record.folder / "codex-trace" / "20261113T000000-critic-abcdef"
        trace.mkdir(parents=True)
        self.bundle = record_test.Bundle(trace).standard()
        records.TraceIngest(record, trace.name, self.bundle.folder).step(final=True)

    def call(self, method, path, token=True):
        headers = {"Origin": self.http.origin, **({"X-TCS-Prover-Token": self.http.token} if token else {})}
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def test_every_route_needs_the_session_token(self):
        for method, path in (("GET", "/records"), ("GET", f"/jobs/{self.name}/model-calls"),
                             ("GET", f"/jobs/{self.name}/model-calls/c000001"),
                             ("GET", f"/jobs/{self.name}/model-calls/c000001/raw"),
                             ("GET", f"/jobs/{self.name}/training-record"), ("DELETE", f"/records/{self.name}")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, token=False)[0], 403)
        self.assertTrue(records.record_folder(self.name).is_dir())

    def test_the_model_calls_routes(self):
        status, _, body = self.call("GET", f"/jobs/{self.name}/model-calls?offset=1&limit=5")
        listing = json.loads(body)
        self.assertEqual((status, listing["total"], [row["id"] for row in listing["calls"]]), (200, 2, ["c000002"]))
        status, _, body = self.call("GET", f"/jobs/{self.name}/model-calls/c000002")
        self.assertEqual((status, json.loads(body)["sent"]["previousCallId"]), (200, "c000001"))
        status, _, body = self.call("GET", f"/jobs/{self.name}/model-calls/c000001/raw")
        self.assertEqual(json.loads(body)["request"].encode(), self.bundle.files["payloads/2.json"])
        self.assertEqual(self.call("GET", f"/jobs/{self.name}/model-calls/c999999")[0], 404)
        self.assertEqual(self.call("GET", "/jobs/no-such-job/model-calls")[0], 404)
        self.assertEqual(self.call("GET", f"/jobs/{self.name}/model-calls?limit=0")[0], 400)

    def test_the_training_record_is_a_zip_download(self):
        status, kind, body = self.call("GET", f"/jobs/{self.name}/training-record")
        self.assertEqual((status, kind), (200, "application/zip"))
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        self.assertIn("requests.jsonl", names)

    def test_a_training_record_that_fails_part_way_arrives_incomplete(self):
        # The browser must not save a cut-short archive as a whole one: the body never gets its last chunk.
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        self.addCleanup(connection.close)
        with mock.patch.object(records.Objects, "names", side_effect=OSError("The item index could not be read.")):
            connection.request("GET", f"/jobs/{self.name}/training-record",
                               headers={"Origin": self.http.origin, "X-TCS-Prover-Token": self.http.token})
            response = connection.getresponse()
            self.assertEqual((response.status, response.getheader("Transfer-Encoding")), (200, "chunked"))
            with self.assertRaises(http.client.IncompleteRead) as cut:
                response.read()
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(cut.exception.partial))

    def test_the_records_list_and_a_delete_that_waits_for_a_working_job(self):
        status, _, body = self.call("GET", "/records")
        self.assertEqual(status, 200)
        self.assertEqual([(row["runFolder"], row["jobExists"]) for row in json.loads(body)], [(self.name, True)])
        self.app.state["phase"] = "running"
        status, _, body = self.call("DELETE", f"/records/{self.name}")
        self.assertEqual(status, 409)
        self.assertTrue(records.record_folder(self.name).is_dir())
        self.app.state["phase"] = "done"
        status, _, body = self.call("DELETE", f"/records/{self.name}")
        self.assertEqual((status, json.loads(body)), (200, {"deleted": self.name}))
        self.assertFalse(records.record_folder(self.name).exists())
        self.assertEqual(self.call("DELETE", f"/records/{self.name}")[0], 404)
        self.assertEqual(self.call("DELETE", "/records/..")[0], 404)


class WebCutoffPlaceholderTests(AppCase):
    def test_a_web_cutoff_run_s_one_shot_calls_are_rows_that_say_why_they_are_not_recorded(self):
        app = self.new_app()
        app.state["retrievalMode"] = "historical"
        app.add_trace({"kind": "request", "stage": "critic", "label": "Critic coordinator", "attempt": 1,
                       "model": "gpt-6.1-sol", "text": "Check."})
        app.add_trace({"kind": "request", "stage": "solve", "label": "Exact solve input", "text": "Prove."})
        app._record_snapshot()
        listing = server.model_calls.call_list(app.run_dir.name)
        self.assertEqual([(row["role"], row["status"], row["placeholderKinds"]) for row in listing["calls"]],
                         [("critic", "not recorded", ["web_cutoff_one_shot"])])
        detail = server.model_calls.call_detail(app.run_dir.name, listing["calls"][0]["id"])
        self.assertEqual(detail["placeholders"][0]["why"], "Not recorded: web-cutoff one-shot calls leave no files.")


if __name__ == "__main__":
    unittest.main()
