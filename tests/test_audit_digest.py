"""Audit time box, fresh-eyes solver brief, and the one-page audit digest; no model calls."""
from pathlib import Path
import tempfile
import threading
import unittest

from ui import audits


class Clock:
    now = 0

    def __call__(self):
        return self.now


def settings(*models, seconds=1):
    return {"intervalHours": seconds / 3600, "models": list(models) + ["none"] * (3 - len(models))}


INITIAL_PROMPT = """Resolve the exact statement below.

Maintain these research records.

STATEMENT:
Prove that every widget graph admits a balanced decomposition in almost-linear time.
"""

INDEX = """# Attempted approaches

## PLAN
- TARGET: balanced decomposition of widget graphs.
- MISSING STEP: construct the balanced decomposition efficiently.

| Node | Parents | Children | Status | Result |
| --- | --- | --- | --- | --- |
| [A006 — Gadget search](A006-gadget-reduction.md) | none | none | RESOLVED | reduction proved |
"""

PROVED = """# Proved lemmas

## L007 — Balanced gadgets expose the optimum

Supports A006.

**Statement.** If k >= 3 then the event has probability at least 1/2.

**Proof.** Markov's inequality on X - 1.

## L019 — Widget packing equals widget covering

**Statement.** The maximum packing number equals the minimum cover number.

**Proof.** LP duality; very long.
"""


class AuditDigestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name)
        (self.run / "APPROACHES").mkdir()
        (self.run / "APPROACHES/index.md").write_text(INDEX)
        (self.run / "APPROACHES/A006-gadget-reduction.md").write_text("# A006 — Gadget search\n\nsecret node body\n")
        (self.run / "PROVED.md").write_text(PROVED)
        self.clock = Clock()
        self.events, self.calls = [], []

    def scheduler(self, provider, config=None):
        instance = audits.ResearchAudits(self.run, on_event=self.events.append, clock=self.clock,
                                        provider=provider, preflight=lambda model: True, config=config)
        self.addCleanup(instance.close)
        return instance

    def launch(self, scheduler, options):
        scheduler.update(options)
        self.clock.now += 1
        scheduler.update(options)
        scheduler.thread.join(timeout=5)
        self.assertFalse(scheduler.thread.is_alive())

    def solver_config(self, **solver):
        base = audits.load_config()
        return {**base, "solver": {**base["solver"], "count": 2, "firstAfterMinutes": 20 / 60,
                                   "intervalMinutes": 60 / 60, "model": "author", **solver}}

    def solver_scheduler(self, provider, config=None, author_model="gpt-6-astra", delivered=None):
        instance = audits.ResearchAudits(self.run, on_event=self.events.append, clock=self.clock,
                                        provider=provider, preflight=lambda model: True,
                                        config=config or self.solver_config(), author_model=author_model,
                                        after_solver_batch=delivered)
        self.addCleanup(instance.close)
        return instance

    def run_solvers(self, scheduler, options, seconds):
        scheduler.update(options)
        self.clock.now += seconds
        scheduler.update(options)
        self.assertIsNotNone(scheduler.solver_thread)
        scheduler.solver_thread.join(timeout=5)
        self.assertFalse(scheduler.solver_thread.is_alive())

    def test_audit_batch_rebuilds_the_digest_from_the_whole_folder(self):
        (self.run / "INITIAL_PROMPT.md").write_text(INITIAL_PROMPT)
        (self.run / "AUDITS").mkdir()
        (self.run / "AUDITS/20260101T000000Z-fresh-eyes-recursion--solver-model-0123abcd.md").write_text(
            "# Fresh-eyes solver [recursion]\n\n## Directions\nDIRECTION 1: shrink the instance by half — size lemma\n")

        def provider(model, prompt, workspace, cancelled):
            self.calls.append((model["value"], prompt, Path(workspace)))
            self.assertNotIn("solver", prompt)
            return {"text": ("Executive summary line.\n\n## Verdicts\nVERDICT A006: STOP — tool node\n"
                             "## Directions\nDIRECTION 1: use existence as a certificate — Lemma X\n"
                             "## Premise gaps\nPREMISE: the decomposition need not be constructed explicitly\n"),
                    "model": model["model"]}

        scheduler = self.scheduler(provider, config=audits.load_config())
        self.launch(scheduler, settings("gpt-6-astra", "gpt-5.6-sol"))
        reports = sorted(path.name for path in (self.run / "AUDITS").glob("*.md"))
        self.assertEqual(len(reports), 3)
        self.assertEqual(len(self.calls), 2)
        digest = (self.run / audits.DIGEST_FILENAME).read_text()
        self.assertIn("A006: STOP x2  <- STOP by 2+ auditors", digest)
        self.assertIn("(fresh-eyes-recursion solver-model) shrink the instance by half", digest)
        self.assertIn("the decomposition need not be constructed explicitly", digest)
        self.assertIn("Executive summary line.", digest)
        self.assertTrue(any(event.get("status") == "digest" for event in self.events))

    def test_seeded_solvers_run_on_their_own_schedule_without_auditors(self):
        (self.run / "INITIAL_PROMPT.md").write_text(INITIAL_PROMPT)
        delivered = []

        def provider(model, prompt, workspace, cancelled):
            self.calls.append((model["value"], prompt, Path(workspace)))
            names = sorted(path.name for path in Path(workspace).iterdir())
            self.assertEqual(names, ["BRIEF.md", "STATEMENT.md"])
            brief = (Path(workspace) / "BRIEF.md").read_text()
            self.assertIn("MISSING STEP", brief)
            self.assertIn("**Statement.** If k >= 3", brief)
            self.assertNotIn("Markov's inequality", brief)
            self.assertNotIn("secret node body", brief)
            self.assertEqual((Path(workspace) / "STATEMENT.md").read_text().strip(),
                             "Prove that every widget graph admits a balanced decomposition in almost-linear time.")
            seed = "certified" if "ITERATIVE CERTIFIED STEP" in prompt else "blackbox"
            return {"text": f"## Directions\nDIRECTION 1: {seed} candidate — first lemma\n", "model": "solver-model"}

        scheduler = self.solver_scheduler(provider, delivered=lambda engine, paths: delivered.append(paths))
        none = settings()
        scheduler.update(none)
        self.clock.now += 10
        scheduler.update(none)
        self.assertIsNone(scheduler.solver_thread, "the first batch waits for firstAfterMinutes")
        self.run_solvers(scheduler, none, 11)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual({call[0] for call in self.calls}, {"gpt-6-astra"})
        self.assertTrue(all("{seed}" not in call[1] for call in self.calls))
        self.assertEqual(len({call[2] for call in self.calls}), 2)
        self.assertTrue(any("ITERATIVE CERTIFIED STEP" in call[1] for call in self.calls))
        self.assertTrue(any("BLACK-BOX PRIMITIVES" in call[1] for call in self.calls))
        reports = sorted(path.name for path in (self.run / "AUDITS").glob("*.md"))
        self.assertEqual(len(reports), 2)
        self.assertTrue(any("-fresh-eyes-certified_step--" in name for name in reports))
        self.assertTrue(any("-fresh-eyes-blackbox_rounding--" in name for name in reports))
        digest = (self.run / audits.DIGEST_FILENAME).read_text()
        self.assertIn("(fresh-eyes-certified_step solver-model) certified candidate", digest)
        self.assertIn("(fresh-eyes-blackbox_rounding solver-model) blackbox candidate", digest)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(sorted(delivered[0]), sorted(f"AUDITS/{name}" for name in reports))
        report = next(path for path in (self.run / "AUDITS").glob("*certified_step*")).read_text()
        self.assertTrue(report.startswith("# Fresh-eyes solver [certified_step] —"))
        self.assertIn("## Technique family\nITERATIVE CERTIFIED STEP", report)
        self.assertEqual(scheduler.checkpoint()["solverBatches"], 1)
        self.assertTrue(scheduler.status()["solverBatches"] == 1 and not scheduler.status()["solverActive"])

        # The next batch waits for intervalMinutes, rotates to the remaining seeds, and keeps
        # the previous batch in AUDITS/ while older batches move to history.
        self.calls.clear()
        self.clock.now += 30
        scheduler.update(none)
        self.assertFalse(scheduler.solver_thread.is_alive())
        self.assertEqual(self.calls, [])
        self.run_solvers(scheduler, none, 31)
        self.assertTrue(any("RECURSION" in call[1] for call in self.calls))
        self.assertTrue(any("STRUCTURAL CHARACTERIZATION" in call[1] for call in self.calls))
        self.assertEqual(len(list((self.run / "AUDITS").glob("*fresh-eyes*"))), 4)
        self.run_solvers(scheduler, none, 61)
        self.assertEqual(len(list((self.run / "AUDITS").glob("*fresh-eyes*"))), 4)
        self.assertEqual(len(list((self.run / "audit_history").glob("*fresh-eyes*"))), 2)
        self.assertEqual(len(delivered), 3)

    def test_solvers_wait_for_an_active_author_and_a_known_model(self):
        (self.run / "INITIAL_PROMPT.md").write_text(INITIAL_PROMPT)

        def provider(model, prompt, workspace, cancelled):
            self.calls.append(model["value"])
            return {"text": "## Directions\nDIRECTION 1: x — y\n", "model": model["model"]}

        # An author model outside the catalog and no auditor: nothing can run.
        unknown = self.solver_scheduler(provider, author_model="not-in-catalog")
        unknown.update(settings())
        self.clock.now += 100
        unknown.update(settings())
        self.assertIsNone(unknown.solver_thread)
        # The auditor slot supplies the model when the author's is unknown; the clock only
        # advances while the author is active.
        fallback = self.solver_scheduler(provider, author_model="not-in-catalog")
        fallback.update(settings("gpt-5.6-sol"), active=False)
        self.clock.now += 100
        fallback.update(settings("gpt-5.6-sol"), active=False)
        self.assertIsNone(fallback.solver_thread)
        self.run_solvers(fallback, settings("gpt-5.6-sol"), 21)
        self.assertEqual(set(self.calls), {"gpt-5.6-sol"})

    def test_solver_batch_is_skipped_without_a_statement_file(self):
        def provider(model, prompt, workspace, cancelled):
            self.calls.append(model["value"])
            return {"text": "VERDICT A006: CONTINUE — one lemma left", "model": model["model"]}

        scheduler = self.solver_scheduler(provider)
        scheduler.update(settings("gpt-6-astra"))
        self.clock.now += 100
        scheduler.update(settings("gpt-6-astra"))
        self.assertIsNone(scheduler.solver_thread)
        scheduler.thread.join(timeout=5)
        self.assertEqual(self.calls, ["gpt-6-astra"])
        self.assertEqual(len(list((self.run / "AUDITS").glob("*.md"))), 1)
        self.assertIn("A006: CONTINUE x1", (self.run / audits.DIGEST_FILENAME).read_text())

    def test_time_box_cancels_a_slow_auditor_without_a_report(self):
        config = {**audits.load_config(), "timeoutMinutes": 0.002, "solver": {"enabled": False}}
        released = threading.Event()

        def provider(model, prompt, workspace, cancelled):
            self.assertTrue(cancelled.wait(5))
            released.set()
            return None

        scheduler = self.scheduler(provider, config=config)
        self.launch(scheduler, settings("gpt-6-astra"))
        self.assertTrue(released.is_set())
        self.assertFalse((self.run / "AUDITS").exists() and list((self.run / "AUDITS").glob("*.md")))
        self.assertTrue(any(event.get("status") == "cancelled" and "Time box" in event.get("text", "")
                            for event in self.events))
        self.assertFalse((self.run / audits.DIGEST_FILENAME).exists())

    def test_config_helpers_have_safe_defaults(self):
        self.assertEqual(audits.timeout_seconds({}), audits.DEFAULT_TIMEOUT_MINUTES * 60)
        self.assertEqual(audits.timeout_seconds({"timeoutMinutes": "bad"}), audits.DEFAULT_TIMEOUT_MINUTES * 60)
        self.assertEqual(audits.timeout_seconds({"timeoutMinutes": 0}), 0)
        self.assertFalse(audits.solver_settings({})["enabled"])
        self.assertFalse(audits.solver_settings({"solver": {"enabled": True}})["enabled"])
        live = audits.solver_settings(audits.load_config())
        self.assertTrue(live["enabled"])
        self.assertEqual(live["model"], "author")
        self.assertIn("WEAKEST object", live["prompt"])
        self.assertIn("{seed}", live["prompt"])
        self.assertEqual(live["count"], 3)
        self.assertEqual(live["firstAfterSeconds"], 20 * 60)
        self.assertEqual(live["intervalSeconds"], 60 * 60)
        self.assertEqual([seed["name"] for seed in live["seeds"]][:2], ["certified_step", "blackbox_rounding"])
        fallback = audits.solver_settings({"solver": {"enabled": True, "count": 9, "seeds": "bad"}, "solver_prompt": "go {seed}"})
        self.assertEqual([seed["name"] for seed in fallback["seeds"]], ["standard_primitives"])
        self.assertEqual(fallback["count"], 1)
        self.assertEqual(fallback["intervalSeconds"], audits.DEFAULT_SOLVER_INTERVAL_MINUTES * 60)
        none = {"intervalHours": 2, "models": ["none", "none", "none"]}
        self.assertTrue(audits.solver_would_run(none, "gpt-6-astra"))
        self.assertFalse(audits.solver_would_run(none, "not-in-catalog"))
        self.assertTrue(audits.solver_would_run({**none, "models": ["gpt-5.6-sol", "none", "none"]}, "not-in-catalog"))
        self.assertFalse(audits.solver_would_run(none, "gpt-6-astra", {**audits.load_config(), "solver": {"enabled": False}}))
        self.assertEqual(audits.report_label("20260101T000000Z-audit-2-gpt-6-astra-0123abcd.md"), "audit-2 gpt-6-astra")
        self.assertEqual(audits.report_label("20260101T000000Z-fresh-eyes-gpt-6-astra-0123abcd.md"), "fresh-eyes gpt-6-astra")
        self.assertEqual(audits.report_label("2026-01-01T000000Z-fresh-eyes-certified_step--gpt-6-astra-0123abcd.md"), "fresh-eyes-certified_step gpt-6-astra")
        self.assertEqual(audits.statement_text(self.run), "")
        (self.run / "INITIAL_PROMPT.md").write_text("no marker here")
        self.assertEqual(audits.statement_text(self.run), "no marker here")


if __name__ == "__main__":
    unittest.main()
