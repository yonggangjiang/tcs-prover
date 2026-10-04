"""Folder-run summaries: per-job metrics, the batch report, and when it is written; no model calls."""

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import workflow_runner as runtime
from ui import batch_summary, cli, server

START = datetime(2026, 10, 2, 8, 0, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def usage(minutes, thread, totals, root=True):
    names = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
    total = dict(zip(names, totals), totalTokens=totals[0] + totals[2])
    return {"kind": "codex_event", "stage": "solve", "node": "author", "root": root, "time": at(minutes),
            "event": {"method": "thread/tokenUsage/updated",
                      "params": {"threadId": thread, "tokenUsage": {"total": total, "last": total}}}}


def quota(minutes, used):
    return {"kind": "codex_event", "stage": "solve", "root": True, "time": at(minutes),
            "event": {"method": "account/rateLimits/updated",
                      "params": {"rateLimits": {"primary": {"usedPercent": used, "resetsAt": 1791566931}}}}}


def item(minutes, kind, **fields):
    return {"kind": "codex_event", "stage": "solve", "root": True, "time": at(minutes),
            "event": {"method": "item/completed", "params": {"item": {"type": kind, **fields}}}}


def critic_call(minutes, verdict, edits="none", tokens=(400_000, 300_000, 40_000, 30_000)):
    """One critic request: its input record, its usage, and its verdict."""

    names = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    return [
        {"kind": "request", "stage": "critic", "label": "Exact critic input", "time": at(minutes - 5),
         "responseSchema": {"type": "object"}, "text": "Review."},
        {"kind": "codex_event", "stage": "critic", "time": at(minutes),
         "event": {"type": "turn.completed", "usage": dict(zip(names, tokens))}},
        {"kind": "critic_result", "stage": "critic", "label": "Critic round", "time": at(minutes),
         "report": str({"verdict": verdict, "solution": "P", "bugs": ""}),
         "text": json.dumps({"verdict": verdict, "edits": edits, "solution": "", "bugs": ""})},
    ]


def status(minutes, label, **fields):
    return {"kind": "status", "stage": fields.pop("stage", "solve"), "label": label, "time": at(minutes), **fields}


SETTINGS = {"authorModel": "gpt-6-astra", "criticModel": "gpt-6-astra", "writerModel": "gpt-6-astra",
            "authorEffort": "max", "criticEffort": "max", "criticRounds": 2, "thinkingHours": 168.0,
            "speedMode": "standard", "reasoningSummary": "concise", "authorWorkflow": "author_critic_cheap",
            "fileManagement": False, "latexWriter": False, "skipStatementReview": True,
            "goalThreadId": "root"}


def solved_records():
    """Reject, then a repaired pass, then an unchanged pass."""

    return [
        status(0, "Workflow step: author"),
        status(0, "Goal started", threadId="root"),
        usage(1, "root", (1_000_000, 800_000, 100_000, 50_000)),
        usage(1.5, "root", (1_000_000, 800_000, 100_000, 50_000)),  # A repeated report.
        usage(2, "child", (500_000, 400_000, 50_000, 20_000), root=False),
        quota(2, 5),
        usage(5, "root", (3_000_000, 2_500_000, 300_000, 150_000)),
        item(6, "contextCompaction"),
        item(6, "commandExecution"),
        item(6, "webSearch"),
        usage(7, "root", (200_000, 100_000, 20_000, 10_000)),  # The counter restarted.
        {"kind": "author_result", "stage": "solve", "time": at(10), "text": "Proof v1"},
        *critic_call(15, "reject"),
        {"kind": "author_result", "stage": "repair", "time": at(20), "text": "Proof v2"},
        *critic_call(25, "pass", "applied"),
        *critic_call(30, "pass"),
        status(30, "Critic approved", stage="critic", text="Round 2 passed without edits."),
        quota(30, 12),
        {"kind": "workflow_result", "stage": "workflow", "time": at(30), "output": "Proof v2"},
    ]


def write_run(runs, name, records, settings=None, source="a.md", files=None):
    run_dir = Path(runs) / name
    run_dir.mkdir(parents=True)
    (run_dir / "job-settings.json").write_text(json.dumps({**SETTINGS, "sourceFile": source, **(settings or {})}))
    (run_dir / "checked-statement.md").write_text(
        f"# Statement sent directly to the proof author\n\n# Problem {name}\n\nEvery task has property P.\n")
    (run_dir / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    for filename, text in (files or {}).items():
        (run_dir / filename).write_text(text)
    return run_dir


class RunMetricsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"

    def test_a_solved_run_counts_every_role_once_and_reads_each_critic_round(self):
        run_dir = write_run(self.runs, "solved", solved_records(),
                            files={"final-proof.md": "**The statement is true.** Proof follows.\n"})
        metrics = batch_summary.run_metrics(run_dir, now=START + timedelta(days=1))
        self.assertEqual((metrics["status"], metrics["solved"]), ("finished", True))
        self.assertEqual(metrics["judge"], ["no", "repaired", "yes"])
        self.assertEqual(metrics["authorSubmissions"], 2)
        self.assertEqual(metrics["answer"], "The statement is true. Proof follows.")
        self.assertEqual(metrics["finalFile"], "final-proof.md")
        self.assertEqual(metrics["title"], "Problem solved")
        roles = metrics["roles"]
        # Repeats are dropped, and a restarted counter keeps what it had counted.
        self.assertEqual(roles["author"], {"input": 3_200_000, "cached": 2_600_000, "output": 320_000,
                                           "reasoning": 160_000, "calls": 3})
        self.assertEqual(roles["subagents"], {"input": 500_000, "cached": 400_000, "output": 50_000,
                                              "reasoning": 20_000, "calls": 1})
        self.assertEqual(roles["critic"], {"input": 1_200_000, "cached": 900_000, "output": 120_000,
                                           "reasoning": 90_000, "calls": 3})
        self.assertEqual(metrics["tokens"]["input"], 4_900_000)
        self.assertEqual(metrics["subagentThreads"], 1)
        # gpt-6-astra: 250 uncached, 25 cached, 1250 output credits per million tokens.
        self.assertEqual(metrics["credits"]["author"], 615.0)
        self.assertEqual(metrics["credits"]["critic"], 75 + 22.5 + 150)
        self.assertEqual(metrics["workingSeconds"], 1800)
        self.assertEqual([metrics["quota"]["first"][1], metrics["quota"]["last"][1]], [5, 12])
        self.assertEqual(metrics["activity"]["compactions"], 1)
        self.assertEqual(metrics["activity"]["webSearches"], 1)
        self.assertEqual(metrics["incompleteRequests"], {})

    def test_fast_speed_and_unknown_models_change_the_credit_estimate(self):
        fast = write_run(self.runs, "fast", solved_records(), settings={"speedMode": "fast"})
        self.assertEqual(batch_summary.run_metrics(fast)["credits"]["author"], 615.0 * 2.5)
        other = write_run(self.runs, "other", solved_records(), settings={"criticModel": "deepseek-v4-pro"})
        credits = batch_summary.run_metrics(other)["credits"]
        self.assertIsNone(credits["critic"])
        self.assertIsNone(credits["total"])
        self.assertFalse(credits["complete"])

    def test_pauses_resumes_and_the_round_limit(self):
        records = [
            status(0, "Workflow step: author"),
            usage(10, "root", (100, 50, 10, 5)),
            {"kind": "workflow_paused", "stage": "solve", "time": at(60), "reason": "Usage quota at 90%."},
            # Four hours later the job is resumed; the pause is not working time.
            status(300, "Resume requested"),
            {"kind": "author_result", "stage": "solve", "time": at(320), "text": "Proof"},
            *critic_call(330, "pass", "applied"),
            status(330, "Critic approved", stage="critic",
                   text="Reached MAX (1 rounds); using the latest repaired proof."),
            {"kind": "workflow_result", "stage": "workflow", "time": at(330), "output": "Proof"},
        ]
        metrics = batch_summary.run_metrics(write_run(self.runs, "resumed", records))
        self.assertEqual((metrics["status"], metrics["solved"], metrics["judge"]), ("finished", True, ["repaired"]))
        self.assertEqual(metrics["workingSeconds"], 60 * 60 + 30 * 60)
        paused = batch_summary.run_metrics(write_run(self.runs, "paused", records[:3]))
        self.assertEqual((paused["status"], paused["solved"], paused["judge"]), ("paused", False, []))
        self.assertEqual(paused["statusDetail"], "Usage quota at 90%.")

    def test_running_unfinished_stopped_and_rejected_jobs(self):
        active = write_run(self.runs, "active", [status(0, "Workflow step: author"), usage(5, "root", (10, 5, 1, 0))])
        self.assertEqual(batch_summary.run_metrics(active, now=START + timedelta(minutes=10))["status"], "running")
        quiet = batch_summary.run_metrics(active, now=START + timedelta(hours=2))
        self.assertEqual(quiet["status"], "unfinished")
        self.assertIn("No activity since", quiet["statusDetail"])
        stopped = write_run(self.runs, "stopped", [
            status(0, "Workflow step: author"), status(5, "Stop requested"), usage(5.1, "root", (10, 5, 1, 0))])
        self.assertEqual(batch_summary.run_metrics(stopped)["status"], "stopped")
        rejected = write_run(self.runs, "rejected", [
            *critic_call(5, "reject"),
            {"kind": "failure_result", "stage": "critic", "time": at(5), "output": "The critic rejected the proof."},
        ], settings={"criticOnly": True})
        metrics = batch_summary.run_metrics(rejected)
        self.assertEqual((metrics["status"], metrics["solved"], metrics["judge"]), ("rejected", False, ["no"]))
        # A request that never reported its usage (killed) is flagged.
        killed = write_run(self.runs, "killed", [critic_call(5, "pass")[0], status(6, "Stop requested")])
        self.assertEqual(batch_summary.run_metrics(killed)["incompleteRequests"], {"critic": 1})


class BatchSummaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"

    def manifest(self, *runs):
        manifest = batch_summary.create_batch(self.runs, folder="jobs", sources=["a.md", "b.md"],
                                              workflow="author_critic_cheap", origin="web",
                                              workflow_options={"compaction_tokens": 150000,
                                                                "subagent_threads": 1})
        for run_id, source in runs:
            batch_summary.add_run(self.runs, manifest["id"], run_id, source)
        return manifest

    def test_the_summary_reports_the_batch_then_each_problem(self):
        write_run(self.runs, "run-a", solved_records(), source="a.md",
                  files={"final-proof.md": "The statement is false.\n"})
        write_run(self.runs, "run-b", [
            status(0, "Workflow step: author"), usage(10, "root", (2_000_000, 1_500_000, 200_000, 100_000)),
            quota(40, 40),
            {"kind": "workflow_paused", "stage": "solve", "time": at(60), "reason": "Usage quota at 90%."},
        ], source="b.md")
        manifest = self.manifest(("run-a", "a.md"), ("run-b", "b.md"))
        path = batch_summary.write_summary(self.runs, manifest["id"])
        text = path.read_text()
        data = json.loads((path.parent / batch_summary.SUMMARY_DATA_FILENAME).read_text())
        self.assertEqual((data["solved"], data["total"]), (1, 2))
        self.assertIn("**1 of 2 problems solved**", text)
        self.assertIn("| Not solved: paused (usage quota) | 1 |", text)
        self.assertIn("| Workflow | author_critic_cheap (cost-optimized) |", text)
        self.assertIn("| Author | gpt-6-astra, effort max |", text)
        self.assertIn("| Critic | gpt-6-astra, effort max, up to 2 rounds |", text)
        self.assertIn("| LaTeX writer | off", text)
        self.assertIn("compaction at 150,000 tokens; 1 subagent at a time", text)
        self.assertIn("| **Total** | **8** | **6,900,000** |", text)
        self.assertIn("went from 5% (", text)
        self.assertIn("to 40%", text)
        self.assertIn("| 1 | a.md | yes | no → repaired → yes | finished | 30 min |", text)
        self.assertIn("| 2 | b.md | no | — | paused (usage quota) | 1 h 00 min |", text)
        self.assertIn("- Answer: The statement is false.", text)
        self.assertIn("- Judge: no → repaired → yes (3 critic rounds; 2 candidates from the author)", text)
        self.assertIn("critic 1.3M in 3 requests", text)
        self.assertIn("Not included: the 1 context compaction. Codex does not report their tokens", text)
        self.assertEqual(data["time"]["workingSeconds"], 1800 + 3600)

    def test_a_continuation_joins_its_problem_and_unchanged_runs_are_not_reread(self):
        write_run(self.runs, "run-a", [
            status(0, "Workflow step: author"), usage(10, "root", (100, 50, 10, 5)),
            {"kind": "workflow_paused", "stage": "solve", "time": at(60), "reason": "Paused."}], source="a.md")
        manifest = self.manifest(("run-a", "a.md"))
        batch_summary.write_summary(self.runs, manifest["id"])
        write_run(self.runs, "run-a2", solved_records(), source="a.md")
        self.assertEqual(batch_summary.register_continuation(self.runs, self.runs / "run-a", "run-a2", "a.md"),
                         [manifest["id"]])
        with mock.patch.object(batch_summary, "run_metrics", wraps=batch_summary.run_metrics) as reads:
            paths = batch_summary.refresh_for_run(self.runs, "run-a2")
        self.assertEqual([call.args[0].name for call in reads.call_args_list], ["run-a2"])
        data = json.loads((paths[0].parent / batch_summary.SUMMARY_DATA_FILENAME).read_text())
        self.assertEqual(len(data["problems"]), 1)
        problem = data["problems"][0]
        self.assertEqual((problem["runs"], problem["status"], problem["solved"]),
                         (["run-a", "run-a2"], "finished", True))
        self.assertEqual(problem["tokens"]["input"], 100 + 4_900_000)
        self.assertIn("`runs/run-a`, then `runs/run-a2`", paths[0].read_text())

    def test_existing_runs_get_a_batch_in_folder_order(self):
        write_run(self.runs, "2026-10-02_10-00-01_b", solved_records(), source="02_b.md")
        write_run(self.runs, "2026-10-02_10-00-00_a", solved_records(), source="01_a.md")
        path = batch_summary.summarize_existing(
            self.runs, [self.runs / "2026-10-02_10-00-01_b", self.runs / "2026-10-02_10-00-00_a"], name="jobs")
        manifest = batch_summary.load_manifest(path.parent)
        self.assertEqual([entry["sourceFile"] for entry in manifest["runs"]], ["01_a.md", "02_b.md"])
        self.assertEqual(manifest["origin"], "manual")
        self.assertTrue(path.parent.name.endswith("_jobs"))
        self.assertEqual(manifest["workflowOptions"],
                         runtime.builtin_workflow("author_critic_cheap")["options"])
        # Naming the batch folder rewrites the same summary.
        self.assertEqual(batch_summary.summarize_existing(self.runs, [path.parent]), path)
        with self.assertRaisesRegex(ValueError, "Not a run folder"):
            batch_summary.summarize_existing(self.runs, [self.runs.parent])


class FolderRunTests(unittest.TestCase):
    """Starting a folder writes the batch; a job that stops working rewrites its summary."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.runs = self.directory / "runs"
        for change in (mock.patch.object(runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(cli.runtime, "configure_standard_streams")):
            change.start()
            self.addCleanup(change.stop)

    def test_a_web_folder_run_writes_its_summary_and_each_finished_job_rewrites_it(self):
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock, manager.fixed_app = self.runs, {}, threading.RLock(), False
        with mock.patch.object(server.subprocess, "Popen", side_effect=lambda *a, **k: mock.Mock(stdin=io.StringIO())):
            started = manager.start_direct_batch({
                "statements": [{"name": "a.md", "statement": "Prove A."}, {"name": "b.md", "statement": "Prove B."}],
                "folder": "jobs", "authorEffort": "max",
            })
        batch_id = started[0].state["batchId"]
        self.assertTrue(batch_id.endswith("_jobs"))
        batch_dir = self.runs / "batches" / batch_id
        manifest = batch_summary.load_manifest(batch_dir)
        self.assertEqual([(entry["runId"], entry["sourceFile"]) for entry in manifest["runs"]],
                         [(app.run_dir.name, name) for app, name in zip(started, ("a.md", "b.md"))])
        self.assertEqual((manifest["origin"], manifest["workflow"]), ("web", "author_critic_cheap"))
        self.assertIn("subagent_threads", manifest["workflowOptions"])
        text = (batch_dir / "summary.md").read_text()
        self.assertIn("**0 of 2 problems solved**", text)
        self.assertIn("2 of 2 jobs are still running", text)
        # The first job finishes: its reader rewrites the summary.
        app = started[0]
        process, token = mock.Mock(), object()
        records = [*critic_call(5, "pass"), status(5, "Critic approved", stage="critic",
                                                  text="Round 1 passed without edits."),
                   {"kind": "workflow_result", "stage": "workflow", "time": at(5), "output": "Proof."}]
        records[2]["report"] = str({"verdict": "pass", "solution": "Proof.", "bugs": ""})
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = 0
        app.process, app.active_token, app.worker_token = process, token, token
        app.state.update(phase="running", stage="critic", activeNode="critic")
        app._read_output(process, token)
        text = (batch_dir / "summary.md").read_text()
        self.assertIn("**1 of 2 problems solved**", text)
        self.assertIn("1 of 2 jobs are still running", text)
        self.assertIn("| 1 | a.md | yes | yes | finished |", text)
        # Invalid folder names are dropped, not trusted.
        self.assertEqual(server.batch_folder_name("../x"), "")
        self.assertEqual(server.batch_folder_name(7), "")

    def test_a_terminal_folder_run_prints_its_summary(self):
        statements = self.directory / "statements"
        statements.mkdir()
        for name in ("b.md", "a.md"):
            (statements / name).write_text(f"Prove {name}.\n")
        errors = io.StringIO()
        with mock.patch.object(server.App, "_launch_solver_locked", autospec=True,
                               return_value=(object(), object())):
            status_code = cli.run_headless_markdown(statements, runs=self.runs, output_stream=io.StringIO(),
                                                    error_stream=errors)
        self.assertEqual(status_code, 0)
        (batch_dir,) = (self.runs / "batches").iterdir()
        manifest = batch_summary.load_manifest(batch_dir)
        self.assertEqual((manifest["folder"], manifest["folderPath"], manifest["origin"]),
                         ("statements", str(statements.resolve()), "terminal"))
        self.assertEqual([entry["sourceFile"] for entry in manifest["runs"]], ["a.md", "b.md"])
        self.assertIn(f"Folder run summary: {batch_dir / 'summary.md'}", errors.getvalue())
        self.assertTrue((batch_dir / "summary.json").is_file())
        # A single statement file is not a folder run.
        single = self.directory / "one.md"
        single.write_text("Prove it.\n")
        with mock.patch.object(server.App, "_launch_solver_locked", autospec=True,
                               return_value=(object(), object())):
            cli.run_headless_markdown(single, runs=self.runs, output_stream=io.StringIO(),
                                      error_stream=io.StringIO())
        self.assertEqual(len(list((self.runs / "batches").iterdir())), 1)

    def test_the_summary_flag_writes_and_prints_a_summary_for_existing_runs(self):
        write_run(self.runs, "run-a", solved_records(), source="a.md")
        output = io.StringIO()
        argv = ["web_ui.py", "--summary", str(self.runs / "run-a"), "--summary-name", "jobs"]
        with mock.patch.object(cli, "RUNS", self.runs), mock.patch.object(cli.sys, "argv", argv), \
                mock.patch.object(cli.sys, "stdout", output):
            self.assertEqual(cli.main(), 0)
        path = Path(output.getvalue().strip())
        self.assertEqual(path.name, "summary.md")
        self.assertIn("# Folder run summary: jobs", path.read_text())
        errors = io.StringIO()
        argv = ["web_ui.py", "--summary", str(self.directory)]
        with mock.patch.object(cli, "RUNS", self.runs), mock.patch.object(cli.sys, "argv", argv), \
                mock.patch.object(cli.sys, "stderr", errors):
            self.assertEqual(cli.main(), 1)
        self.assertIn("Cannot write the summary: Not a run folder", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
