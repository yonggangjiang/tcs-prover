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
Design an almost-linear exact directed global min-cut algorithm.
"""

INDEX = """# Attempted approaches

## PLAN
- TARGET: exact directed global min cut.
- MISSING STEP: sample one arborescence with marginals at most (3/2)c/lambda.

| Node | Parents | Children | Status | Result |
| --- | --- | --- | --- | --- |
| [A006 — Tree search](A006-tree-packing-reduction.md) | none | none | RESOLVED | reduction proved |
"""

PROVED = """# Proved lemmas

## L007 — Feasible arborescence packing exposes a minimum cut

Supports A006.

**Statement.** If P >= 2rho/3 then Pr(X=1) >= 1/2.

**Proof.** Markov's inequality on X - 1.

## L019 — Fractional arborescence packing equals the rooted minimum cut

**Statement.** The maximum packing mass equals rho.

**Proof.** LP duality; very long.
"""


class AuditDigestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name)
        (self.run / "APPROACHES").mkdir()
        (self.run / "APPROACHES/index.md").write_text(INDEX)
        (self.run / "APPROACHES/A006-tree-packing-reduction.md").write_text("# A006 — Tree search\n\nsecret node body\n")
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

    def test_solver_sees_only_statement_and_brief_and_digest_merges_everything(self):
        (self.run / "INITIAL_PROMPT.md").write_text(INITIAL_PROMPT)

        def provider(model, prompt, workspace, cancelled):
            self.calls.append((model["value"], prompt, Path(workspace)))
            if "solver" in prompt:
                names = sorted(path.name for path in Path(workspace).iterdir())
                self.assertEqual(names, ["BRIEF.md", "STATEMENT.md"])
                brief = (Path(workspace) / "BRIEF.md").read_text()
                self.assertIn("MISSING STEP", brief)
                self.assertIn("**Statement.** If P >= 2rho/3", brief)
                self.assertNotIn("Markov's inequality", brief)
                self.assertNotIn("secret node body", brief)
                self.assertEqual((Path(workspace) / "STATEMENT.md").read_text().strip(),
                                 "Design an almost-linear exact directed global min-cut algorithm.")
                return {"text": "## Directions\nDIRECTION 1: iterate one tree by a circulation — prove the half-step lemma\n",
                        "model": "solver-model"}
            return {"text": ("Executive summary line.\n\n## Verdicts\nVERDICT A006: STOP — tool node\n"
                             "## Directions\nDIRECTION 1: use existence as a certificate — Lemma X\n"
                             "## Premise gaps\nPREMISE: the packing need not be constructed\n"),
                    "model": model["model"]}

        config = audits.load_config()
        scheduler = self.scheduler(provider, config=config)
        self.launch(scheduler, settings("gpt-6-astra", "gpt-5.6-sol"))
        reports = sorted(path.name for path in (self.run / "AUDITS").glob("*.md"))
        self.assertEqual(len(reports), 3)
        self.assertEqual(sum("fresh-eyes" in name for name in reports), 1)
        solver_calls = [call for call in self.calls if "solver" in call[1]]
        self.assertEqual(len(solver_calls), 1)
        self.assertEqual(solver_calls[0][0], "gpt-6-astra")
        self.assertNotEqual(solver_calls[0][2], self.run)
        digest = (self.run / audits.DIGEST_FILENAME).read_text()
        self.assertIn("A006: STOP x2  <- STOP by 2+ auditors", digest)
        self.assertIn("(fresh-eyes solver-model) iterate one tree by a circulation", digest)
        self.assertIn("the packing need not be constructed", digest)
        self.assertIn("Executive summary line.", digest)
        self.assertTrue(any(event.get("status") == "digest" for event in self.events))
        solver_report = next(path for path in (self.run / "AUDITS").glob("*fresh-eyes*"))
        self.assertTrue(solver_report.read_text().startswith("# Fresh-eyes solver —"))

    def test_solver_is_skipped_without_a_statement_file(self):
        def provider(model, prompt, workspace, cancelled):
            self.calls.append(model["value"])
            return {"text": "VERDICT A006: CONTINUE — one lemma left", "model": model["model"]}

        scheduler = self.scheduler(provider)
        self.launch(scheduler, settings("gpt-6-astra"))
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
        self.assertEqual(live["model"], "slot-1")
        self.assertIn("WEAKEST object", live["prompt"])
        self.assertEqual(audits.statement_text(self.run), "")
        (self.run / "INITIAL_PROMPT.md").write_text("no marker here")
        self.assertEqual(audits.statement_text(self.run), "no marker here")


if __name__ == "__main__":
    unittest.main()
