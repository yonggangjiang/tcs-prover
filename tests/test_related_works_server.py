"""Related works on the server: uploads, job creation, additions and their notices, folder batches (no models)."""

import errno
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import runtime as retrieval_runtime
import workflow_runner as runtime
from ui import related_works, retrieval, server

STAGES = {"review": False, "author": True, "critic": True, "writing": True}
CUTOFF = "2026-10-02T14:13:12Z"


class Stdin(io.StringIO):
    text = None

    def close(self):
        self.text = self.getvalue()
        super().close()


class FakeRunner:
    """A runner that has already ended, with no PID to signal; its stdout is ``lines``."""

    def __init__(self, lines=()):
        self.stdin = Stdin()
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.pid = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def inode(path):
    info = os.stat(path)
    return info.st_dev, info.st_ino


class RelatedWorksCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.runs = self.root / "runs"
        self.manager = object.__new__(server.Server)
        self.manager.runs, self.manager.jobs, self.manager.jobs_lock = self.runs, {}, threading.RLock()
        self.manager.fixed_app, self.manager._retry_stop = False, threading.Event()
        self.launches, self.runners, self.spawned = [], [], []
        for change in (patch.object(runtime, "verify_model_credentials"),
                       patch.object(server, "grant_windows_access"),
                       patch.object(server.App, "_spawn_worker", autospec=True, side_effect=self.spawn),
                       patch.object(server.subprocess, "Popen", side_effect=self.popen)):
            change.start()
            self.addCleanup(change.stop)
        self.stage = server.staged_uploads(self.runs)

    def popen(self, argv, **kwargs):
        folder = Path(kwargs["cwd"]) / related_works.FOLDER
        # What the author's workspace held when its runner started.
        self.launches.append((argv, Path(kwargs["cwd"]), sorted(path.name for path in folder.iterdir())
                              if folder.is_dir() else None))
        return self.runners.pop(0) if self.runners else FakeRunner()

    def spawn(self, app, target, args, token):
        app.worker_token = token
        self.spawned.append((target, args))

    def upload(self, name="paper.txt", data=b"Lemma 1 holds.\n"):
        return self.stage.receive(name, io.BytesIO(data), len(data))["token"]

    def note(self, title, text):
        return self.stage.note(title, text)["token"]

    def expired(self, token):
        try:
            self.stage.get(token)
        except related_works.RelatedWorksError:
            return True
        return False

    def start(self, **extra):
        return self.manager.start_workflow({"content": "Prove S.", "workflowStages": dict(STAGES), **extra})

    def run_folders(self):
        return sorted(path.name for path in self.runs.iterdir() if not path.name.startswith(".")) \
            if self.runs.is_dir() else []

    def paused(self, app, thread="thread-1"):
        """Settle ``app``'s running author into an error pause (no automatic resume)."""

        token = object()
        process = FakeRunner([{"kind": "workflow_paused", "stage": "solve", "node": "author", "threadId": thread,
                               "state": {"statement": "Prove S.", "paused": True}, "reason": "Connection lost"}])
        app.state.update(goalThreadId=thread)
        app.process, app.active_token, app.worker_token = process, token, token
        app._read_output(process, token)
        app.state["autoResumeAt"] = 0
        return app

    @staticmethod
    def labels(app):
        return [record.get("label") for record in app.state["trace"]]


class CreationTests(RelatedWorksCase):
    def test_the_works_are_in_the_workspace_before_the_author_starts(self):
        paper, idea = self.upload("A paper.txt"), self.note("Idea", "Try induction.")
        app = self.start(relatedWorks=[paper, idea])
        argv, cwd, present = self.launches[-1]
        self.assertEqual(cwd, app.run_dir)
        self.assertEqual(present, ["A-paper.txt", "INDEX.md", "Idea.md"])
        self.assertIn("related_works=true", argv)
        self.assertEqual([item["name"] for item in app.snapshot()["relatedWorks"]["items"]], ["A-paper.txt", "Idea.md"])
        record = next(record for record in app.state["trace"] if record.get("label") == "Related works added")
        self.assertEqual(record["stage"], server.RELATED_WORKS_STAGE)
        self.assertEqual(record["relatedWorks"], [{"name": "A-paper.txt", "originalName": "A paper.txt",
                                                   "sizeText": "15 B"},
                                                  {"name": "Idea.md", "originalName": "", "sizeText": "23 B"}])
        # The uploads go once the job exists; the manifest is the controller's.
        self.assertTrue(self.expired(paper) and self.expired(idea))
        self.assertTrue((server.controller_directory(app.run_dir) / related_works.MANIFEST_NAME).is_file())
        self.assertEqual(self.manager.jobs, {app.state["runId"]: app})
        state = app.snapshot()["relatedWorks"]
        self.assertEqual((state["available"], state["reason"], state["count"], state["batch"]), (True, "", 2, None))

    def test_a_job_without_related_works_gets_no_block(self):
        self.start()
        self.assertNotIn("related_works=true", self.launches[-1][0])
        self.assertIsNone(self.launches[-1][2])

    def test_refusals_come_before_anything_is_created(self):
        token = self.upload()
        cases = (({"relatedWorks": ["missing-token-0000000000"]}, related_works.EXPIRED),
                 ({"relatedWorks": "not a list"}, "Send the related works as a list of 1 to 200 upload tokens."),
                 ({"relatedWorks": []}, "Send the related works as a list of 1 to 200 upload tokens."),
                 ({"relatedWorks": [token], "workflowStages": {**STAGES, "author": False}},
                  "Related works are read by the proof author; turn on the Author stage or remove them."))
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError) as refused:
                    self.start(**extra)
                self.assertEqual(str(refused.exception), message)
                self.assertEqual(self.run_folders(), [])
        reviewed = server.App(runs=self.runs)
        self.manager.jobs["reviewed"] = reviewed
        with self.assertRaises(ValueError) as refused:
            self.manager.start_workflow({"content": "Prove S.", "relatedWorks": [token]}, "reviewed")
        self.assertEqual(str(refused.exception), "Add related works on the job page.")
        for start in (self.manager.start_direct_job, self.manager.start_job):
            with self.assertRaises(ValueError):
                start({"statement": "Prove S.", "relatedWorks": [token]})
        self.assertFalse(self.expired(token))
        self.assertEqual(self.run_folders(), [])

    def test_a_failed_creation_removes_its_folder_and_keeps_the_uploads(self):
        token = self.upload()
        with patch.object(server.subprocess, "Popen", side_effect=OSError("cannot start")):
            with self.assertRaisesRegex(OSError, "cannot start"):
                self.start(relatedWorks=[token])
        self.assertEqual(self.run_folders(), [])
        self.assertFalse(self.expired(token))
        self.start(relatedWorks=[token])
        self.assertTrue(self.expired(token))

    def test_a_review_retry_in_a_new_run_copies_the_related_works(self):
        source = self.manager.start_workflow({"content": "Prove S.", "workflowStages": {**STAGES, "review": True},
                                              "relatedWorks": [self.upload()]})
        with source.lock:
            source.state.update(phase="input", error="Review failed: offline.")
        copy = server.App(runs=self.runs, load_latest=False)
        copy.start_review("Prove S.", workflow_stages={**STAGES, "review": True},
                          continuation_source=source.run_dir, stopped_stage="review")
        self.assertNotEqual(copy.run_dir, source.run_dir)
        self.assertEqual((copy.run_dir / "related-works" / "paper.txt").read_bytes(), b"Lemma 1 holds.\n")
        self.assertNotEqual(inode(copy.run_dir / "related-works" / "paper.txt"),
                            inode(source.run_dir / "related-works" / "paper.txt"))
        self.assertIn("Related works added", self.labels(copy))


class WebCutoffTests(RelatedWorksCase):
    def setUp(self):
        super().setUp()
        self.codex = self.root / "codex"
        self.codex.write_bytes(b"\xcf\xfa\xed\xfe" + b"unit-test-native-binary")
        self.codex.chmod(0o700)
        real_prepare = retrieval.prepare_policy

        def prepare(workspace, cutoff):
            return real_prepare(workspace, cutoff, controller_root=self.root / "controller")

        for change in (patch.object(retrieval_runtime, "codex_executable", return_value=str(self.codex)),
                       patch.object(retrieval, "capabilities", return_value={"historicalAvailable": True}),
                       patch.object(retrieval, "prepare_policy", side_effect=prepare),
                       patch.object(retrieval, "preflight_policy"), patch.object(retrieval, "preflight_compiler")):
            change.start()
            self.addCleanup(change.stop)
        retrieval._CACHE.clear()

    def audit(self, app):
        policy = retrieval_runtime.load_policy(app.state["retrievalPolicyPath"])
        lines = Path(policy["broker"]["auditLog"]).read_text().splitlines()
        return [json.loads(line) for line in lines if json.loads(line).get("event") == "related_works_added"]

    def test_a_web_cutoff_run_takes_related_works_into_its_own_workspace_with_an_audit_record(self):
        app = self.start(relatedWorks=[self.upload()], retrievalMode="historical", retrievalCutoffUtc=CUTOFF)
        policy = retrieval_runtime.load_policy(app.state["retrievalPolicyPath"])
        self.assertEqual(Path(policy["isolation"]["workspace"]), app.run_dir)
        argv, cwd, present = self.launches[-1]
        self.assertEqual(present, ["INDEX.md", "paper.txt"])
        self.assertIn("related_works=true", argv)
        [record] = self.audit(app)
        self.assertEqual((record["source"], record["cutoff"]), ("controller", CUTOFF))
        [entry] = record["relatedWorks"]
        self.assertEqual((entry["name"], entry["size"], entry["kind"]), ("paper.txt", 15, "file"))
        self.assertEqual(entry["sha256"], related_works.hashlib.sha256(b"Lemma 1 holds.\n").hexdigest())
        # The isolation checks still pass: the folder is an ordinary input of the run.
        retrieval.check_run(app.state["retrievalPolicyPath"], app.run_dir, controller_root=self.root / "controller")
        # Additions during the run are audited too, and a failed audit adds nothing.
        app.state.update(phase="prepared")
        app.add_related_works([self.upload("second.txt", b"More.\n")], self.stage)
        self.assertEqual([record["relatedWorks"][0]["name"] for record in self.audit(app)],
                         ["paper.txt", "second.txt"])
        third = self.upload("third.txt", b"Third.\n")
        with patch.object(retrieval_runtime, "controller_audit",
                          side_effect=retrieval_runtime.IsolationCheckUnavailable("log refused")):
            with self.assertRaisesRegex(ValueError, "Could not add the related works"):
                app.add_related_works([third], self.stage)
        self.assertFalse((app.run_dir / "related-works" / "third.txt").exists())
        self.assertFalse(self.expired(third))


class NoticeTests(RelatedWorksCase):
    def setUp(self):
        super().setUp()
        self.app = self.start()

    def steers(self):
        path = self.app.run_dir / server.AUTHOR_STEER_FILENAME
        return [item["instruction"] for item in json.loads(path.read_text())["queue"]] if path.exists() else []

    def test_a_running_author_is_told_at_its_next_step(self):
        self.app.state.update(phase="running", stage="critic", activeNode="critic")
        self.app.add_related_works([self.upload()], self.stage)
        notice = related_works.notice_text(["paper.txt"])
        self.assertEqual(self.steers(), [notice])
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["text"], record["relatedWorks"], record["node"]),
                         ("Live instruction queued", notice, ["paper.txt"], "critic"))
        self.assertEqual(self.app.state["activeNode"], "critic")

    def test_before_the_first_launch_the_first_prompt_points_to_them(self):
        for phase in ("reviewing", "reviewed", "prepared"):
            with self.subTest(phase=phase):
                self.app.state.update(phase=phase)
                self.app.add_related_works([self.upload(f"{phase}.txt")], self.stage)
                self.assertEqual((self.steers(), self.app.state["relatedWorksPending"]), ([], []))

    def test_finished_and_author_less_jobs_refuse_them(self):
        token = self.upload()
        self.app.state.update(phase="done")
        with self.assertRaises(ValueError) as refused:
            self.app.add_related_works([token], self.stage)
        self.assertEqual(str(refused.exception), "This job has finished; continue it to add related works.")
        self.app.state.update(phase="paused", workflowStages={**STAGES, "author": False})
        with self.assertRaises(ValueError) as refused:
            self.app.add_related_works([token], self.stage)
        self.assertEqual(str(refused.exception),
                         "Related works are read by the proof author; this job has no Author stage.")
        self.assertFalse(self.expired(token))

    def test_a_paused_author_is_told_with_the_next_message_or_at_resume_once(self):
        self.paused(self.app)
        self.app.add_related_works([self.upload()], self.stage)
        self.assertEqual((self.steers(), self.app.state["relatedWorksPending"]), ([], ["paper.txt"]))
        saved = json.loads((server.controller_directory(self.app.run_dir) / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(saved["relatedWorksPending"], ["paper.txt"])
        # The next message carries the notice; once answered, it is not sent again.
        self.app.ask_author("Did you read it?")
        self.runners.append(FakeRunner([{"kind": "conversation_reply", "text": "Yes."}]))
        self.assertTrue(self.app.start_conversation_if_due())
        request = json.loads(self.runner_stdin())
        self.assertEqual(request["notice"], related_works.notice_text(["paper.txt"]))
        target, args = self.spawned[-1]
        target(*args)
        self.assertEqual(self.app.state["relatedWorksPending"], [])
        # A notice not yet delivered goes into the queue at Resume.
        self.app.add_related_works([self.upload("later.txt")], self.stage)
        self.app.resume()
        self.assertEqual(self.steers(), [related_works.notice_text(["later.txt"])])
        self.assertEqual(self.app.state["relatedWorksPending"], [])
        record = next(record for record in reversed(self.app.state["trace"])
                      if record.get("label") == "Live instruction queued")
        self.assertEqual(record["relatedWorks"], ["later.txt"])

    def runner_stdin(self):
        return self.spawned[-1][1][0].stdin.text


class FolderBatchTests(RelatedWorksCase):
    def batch(self, *tokens, names=("a.md", "b.md")):
        body = {"statements": [{"name": name, "statement": f"Prove {name}."} for name in names],
                "workflowStages": dict(STAGES)}
        if tokens:
            body["relatedWorks"] = list(tokens)
        return self.manager.start_direct_batch(body)

    def store(self, batch_id):
        return server.folder_batch_store(self.runs, batch_id)

    def test_every_job_links_the_one_batch_copy(self):
        token = self.upload()
        self.manager.installation = {"maxRunningJobs": 1}  # The second job waits in the queue.
        started = self.batch(token)
        batch_id = started.folder_batch
        self.assertRegex(batch_id, r"\Ab_[0-9a-f]{10}\Z")
        stored = self.store(batch_id).root / "related-works" / "paper.txt"
        self.assertFalse(stored.is_relative_to(started[0].run_dir))
        self.assertTrue(self.expired(token))
        for app in started:
            with self.subTest(job=app.state["sourceFile"]):
                linked = app.run_dir / "related-works" / "paper.txt"
                self.assertEqual(inode(linked), inode(stored))
                self.assertIn("paper.txt", (app.run_dir / "related-works" / "INDEX.md").read_text())
                state = app.snapshot()
                self.assertEqual(state["folderBatch"], batch_id)
                self.assertEqual([item["batch"] for item in state["relatedWorks"]["items"]], [batch_id])
                self.assertEqual(state["relatedWorks"]["batch"]["id"], batch_id)
                saved = json.loads((server.controller_directory(app.run_dir)
                                    / server.JOB_SETTINGS_FILENAME).read_text())
                self.assertEqual(saved["folderBatch"], batch_id)
        self.assertEqual(os.stat(stored).st_nlink, 3)
        # A restart keeps the batch, and the records of a queued job keep it queued.
        self.assertEqual(started[1].state["phase"], "prepared")
        restored = server.restore_saved_app(server.App(started[1].trace_file, self.runs))
        self.assertEqual((restored.state["phase"], restored.state["folderBatch"]), ("prepared", batch_id))

    def test_a_job_that_cannot_link_gets_its_own_copy_and_says_so(self):
        token = self.upload()
        real_link = os.link

        def refuse(source, target, *args, **kwargs):
            if "dst_dir_fd" in kwargs and str(source) == "paper.txt":
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_link(source, target, *args, **kwargs)

        with patch.object(related_works.os, "link", side_effect=refuse):
            started = self.batch(token, names=("a.md",))
        app = started[0]
        stored = self.store(started.folder_batch).root / "related-works" / "paper.txt"
        linked = app.run_dir / "related-works" / "paper.txt"
        self.assertEqual(linked.read_bytes(), stored.read_bytes())
        self.assertNotEqual(inode(linked), inode(stored))
        diagnostic = next(record for record in app.state["trace"] if record.get("kind") == "diagnostic")
        self.assertIn("could not be hard-linked", diagnostic["text"])
        self.assertIn("paper.txt", diagnostic["text"])

    def test_additions_during_the_run_reach_every_job_with_one_notice_each(self):
        started = self.batch(names=("a.md", "b.md", "c.md"))
        running, paused, queued = started
        for app in started:
            app.state.update(phase="running", stage="solve", activeNode="author")
        self.paused(paused)
        queued.state.update(phase="prepared")
        # A continuation sharing the running job's workspace is linked once, with it.
        follower = server.App(runs=self.runs, load_latest=False)
        follower.start_direct_statement("Prove a.md.", workflow_stages=dict(STAGES),
                                        continuation_source=running.run_dir, stopped_stage="solve")
        self.manager.jobs[follower.state["runId"]] = follower
        follower.state.update(phase="done")
        result = self.manager.add_batch_related_works([self.upload()], started.folder_batch)
        self.assertEqual(result["added"], ["paper.txt"])
        self.assertEqual((result["count"], result["available"]), (1, True))
        stored = self.store(started.folder_batch).root / "related-works" / "paper.txt"
        for app in (running, paused, queued):
            self.assertEqual(inode(app.run_dir / "related-works" / "paper.txt"), inode(stored))
        self.assertFalse((follower.run_dir / "related-works").exists())
        notices = {row["runId"]: row["notice"] for row in result["results"]}
        self.assertEqual(notices, {running.state["runId"]: "instruction", paused.state["runId"]: "pending",
                                   queued.state["runId"]: "", follower.state["runId"]: ""})
        steer = json.loads((running.run_dir / server.AUTHOR_STEER_FILENAME).read_text())["queue"]
        self.assertEqual([item["instruction"] for item in steer], [related_works.notice_text(["paper.txt"])])
        self.assertEqual(paused.state["relatedWorksPending"], ["paper.txt"])
        self.assertFalse((queued.run_dir / server.AUTHOR_STEER_FILENAME).exists())
        for app in (running, paused, queued, follower):
            self.assertEqual(self.labels(app).count("Related works added"), 1)
        # The listing, also by one of the batch's jobs.
        listing = self.manager.batch_related_works(run_id=paused.state["runId"])
        self.assertEqual(([item["name"] for item in listing["items"]], listing["id"]),
                         (["paper.txt"], started.folder_batch))
        self.assertEqual(sorted(listing["runIds"]), sorted(app.state["runId"] for app in (*started, follower)))

    def test_a_job_that_does_not_get_the_additions_is_named_in_the_notice(self):
        started = self.batch(names=("a.md", "b.md"))
        here, elsewhere = started
        elsewhere.state["runningElsewhere"] = "Another TCS Prover process runs this job."
        result = self.manager.add_batch_related_works([self.upload()], started.folder_batch)
        self.assertTrue((here.run_dir / "related-works" / "paper.txt").is_file())
        self.assertFalse((elsewhere.run_dir / "related-works" / "paper.txt").exists())
        problems = {row["runId"]: row["problem"] for row in result["results"]}
        self.assertEqual(problems, {here.state["runId"]: "",
                                    elsewhere.state["runId"]: "Another TCS Prover process runs this job."})
        # The page shows the notice only: it names the job that did not get them.
        self.assertEqual(result["notice"], "Could not add them to every job: b.md: Another TCS Prover process "
                                           "runs this job.")

    def test_a_review_retry_of_a_batch_job_links_the_batch_copy_again(self):
        started = self.batch(self.upload(), names=("a.md",))
        source = started[0]
        source.state.update(phase="input", error="Review failed.")
        copy = server.App(runs=self.runs, load_latest=False)
        copy.start_review("Prove a.md.", workflow_stages={**STAGES, "review": True},
                          continuation_source=source.run_dir, stopped_stage="review")
        stored = self.store(started.folder_batch).root / "related-works" / "paper.txt"
        self.assertEqual(inode(copy.run_dir / "related-works" / "paper.txt"), inode(stored))
        self.assertEqual(copy.state["folderBatch"], started.folder_batch)

    def test_a_finished_batch_refuses_and_an_unknown_one_is_named(self):
        started = self.batch(names=("a.md",))
        started[0].state.update(phase="done")
        token = self.upload()
        with self.assertRaises(ValueError) as refused:
            self.manager.add_batch_related_works([token], started.folder_batch)
        self.assertEqual(str(refused.exception), "Every job of this batch has finished; continue one to add "
                                                 "related works.")
        for batch_id in ("b_0123456789", "../x", "__root__"):
            with self.assertRaises(ValueError):
                self.manager.add_batch_related_works([token], batch_id)
        self.assertFalse(self.expired(token))


class UploadRouteTests(RelatedWorksCase):
    def setUp(self):
        super().setUp()
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.http.shutdown)

    def post(self, path, body=b"", content_type="application/octet-stream", headers=None, token=True, chunked=False):
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=10)
        values = {"Content-Type": content_type, **({"X-TCS-Prover-Token": self.http.token} if token else {}),
                  **(headers or {})}
        try:
            if chunked:
                connection.request("POST", path, body=iter([body]), headers=values, encode_chunked=True)
            else:
                connection.request("POST", path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def get(self, path):
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=10)
        try:
            connection.request("GET", path, headers={"X-TCS-Prover-Token": self.http.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_files_are_staged_as_raw_bytes(self):
        self.assertEqual(self.post("/uploads?name=a.txt", b"Lemma.", token=False)[0], 403)
        status, value = self.post("/uploads?name=a.txt", b"Lemma.", content_type="application/json")
        self.assertEqual((status, value["error"]), (400, "Send the file's bytes as application/octet-stream."))
        status, value = self.post("/uploads?name=a.txt", b"Lemma.", chunked=True)
        self.assertEqual(status, 400)
        self.assertIn("without transfer encoding", value["error"])
        self.assertEqual(self.post("/uploads?name=a.txt&batch=old", b"Lemma.")[0], 403)
        status, value = self.post("/uploads?name=", b"Lemma.")
        self.assertEqual((status, value["error"]), (400, "Give the file a name."))
        with patch.object(related_works, "MAX_FILE_BYTES", 4):
            status, value = self.post("/uploads?name=big.txt", b"Lemma.")
        self.assertEqual(status, 413)
        self.assertIn("MB per file.", value["error"])
        status, value = self.post("/uploads?name=My%20paper.txt", b"Lemma.")
        self.assertEqual(status, 200)
        upload = value["upload"]
        self.assertEqual((upload["name"], upload["originalName"], upload["size"], upload["kind"]),
                         ("My-paper.txt", "My paper.txt", 6, "file"))
        self.assertFalse(self.expired(upload["token"]))
        status, value = self.post("/uploads/note", json.dumps({"title": "Idea", "text": "Induct."}).encode(),
                                  content_type="application/json")
        self.assertEqual((status, value["upload"]["kind"], value["upload"]["name"]), (200, "note", "Idea.md"))
        status, value = self.post("/uploads/note", json.dumps({"text": " "}).encode(), content_type="application/json")
        self.assertEqual((status, value["error"]), (400, "Enter the note's text."))
        for expected in (True, False):
            status, value = self.post("/uploads/discard", json.dumps({"token": upload["token"]}).encode(),
                                      content_type="application/json")
            self.assertEqual((status, value), (200, {"discarded": expected}))

    def test_job_and_batch_routes(self):
        started = self.http.start_direct_batch({"statements": [{"name": "a.md", "statement": "Prove A."}],
                                                "workflowStages": dict(STAGES)})
        app = started[0]
        app.state.update(phase="prepared")
        token = self.upload()
        status, state = self.post(f"/related-works/add?job={app.state['runId']}",
                                  json.dumps({"uploads": [token]}).encode(), content_type="application/json")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in state["relatedWorks"]["items"]], ["paper.txt"])
        batch = started.folder_batch
        status, value = self.post(f"/batch-related-works/add?batch={batch}",
                                  json.dumps({"uploads": [self.upload("shared.txt")]}).encode(),
                                  content_type="application/json")
        self.assertEqual(status, 200)
        self.assertEqual((value["id"], value["added"], value["count"]), (batch, ["shared.txt"], 1))
        status, value = self.get(f"/batch-related-works?batch={batch}")
        self.assertEqual((status, [item["name"] for item in value["items"]]), (200, ["shared.txt"]))
        status, value = self.get(f"/batch-related-works?job={app.state['runId']}&batch=__root__")
        self.assertEqual((status, value["id"]), (200, batch))
        self.assertEqual(self.get("/batch-related-works?batch=b_0000000000")[0], 400)
        state = self.http.get_job(app.state["runId"]).snapshot()
        self.assertEqual([item["name"] for item in state["relatedWorks"]["items"]], ["paper.txt", "shared.txt"])
        self.assertEqual(state["relatedWorks"]["batch"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
