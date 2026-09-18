"""The capped record tool the author uses instead of cat."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import records  # noqa: E402


INDEX = """# Attempted approaches

## PLAN
- TARGET: exact global directed min cut in almost-linear time.
- MISSING STEP: sample one arborescence with marginals at most (3/2)c/lambda.

| Node | Parents | Children | Status | Result or remaining question |
| --- | --- | --- | --- | --- |
| [A001 — Verified benchmarks](A001-verified-benchmarks.md) | none | A004 | RESOLVED | Open problem noted. |
| [A006 — Tree search](A006-tree-packing-reduction.md) | A002 | A007 | RESOLVED | Conditional reduction proved. |
| [A019 — Dynamic trees](A019-dynamic-threshold-trees.md) | A006 | none | ACTIVE | Selection and total maintenance remain unproved. |
"""

NODE = """# A006 — Tree search and a sufficient packing primitive
Abstract: the centroid search plus the one-crossing bound reduce the task to a sampler.
Parents: [A002](A002-rooted-flow-reductions.md).
Children: none.
Status: RESOLVED.
Closes the MISSING step: no.

## Context and objective

Backgrounds here.

## Detailed work

""" + "\n".join(f"Line {i} of detailed work." for i in range(1, 200)) + """

## Conclusions

L006 and L007 prove the reduction.
"""

PROVED = """# Proved lemmas

## L006 — A tree-connectivity promise reduces to small total flow input

**Statement.** Let T be any undirected tree. Then the centroid search finds the cut.

**Proof.** Remove r and recurse on centroids. Done.

## L007 — Feasible arborescence packing exposes a minimum cut

**Statement.** If P >= 2rho/3 then Pr(X=1) >= 1/2.

**Proof.** Markov.
"""


class RecordsToolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name)
        (self.run / "APPROACHES").mkdir()
        (self.run / "APPROACHES/index.md").write_text(INDEX)
        (self.run / "APPROACHES/A006-tree-packing-reduction.md").write_text(NODE)
        (self.run / "PROVED.md").write_text(PROVED)
        (self.run / "AUDITS").mkdir()
        (self.run / "AUDITS/report.md").write_text("# Audit-1\n\nVERDICT A006: STOP — tool node\n")

    def run_tool(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            records.main(["--dir", str(self.run), *argv])
        return stream.getvalue()

    def test_overview_shows_plan_rows_lemma_titles_and_audit_state(self):
        text = self.run_tool("overview")
        self.assertIn("MISSING STEP", text)
        self.assertIn("A006 [RESOLVED] Tree search", text)
        self.assertIn("Settled nodes", text)
        self.assertIn("A019 [ACTIVE] Dynamic trees :: Selection and total maintenance remain unproved.", text)
        self.assertIn("L006 — A tree-connectivity promise", text)
        self.assertIn("digest absent; 1 report(s)", text)
        self.assertNotIn("Line 5 of detailed work", text)

    def test_node_defaults_to_header_and_objective_and_caps_sections(self):
        text = self.run_tool("node", "A006")
        self.assertIn("Abstract:", text)
        self.assertIn("Backgrounds here.", text)
        self.assertNotIn("Line 1 of detailed work", text)
        detailed = self.run_tool("node", "a006", "--section", "detailed", "--lines", "5")
        self.assertIn("Line 5 of detailed work", text := detailed)
        self.assertNotIn("Line 6 of detailed work", text)
        self.assertIn("more lines in section", text)
        self.assertIn("No node file for A999", self.run_tool("node", "A999"))

    def test_lemma_prints_statement_only_unless_full(self):
        text = self.run_tool("lemma", "L007")
        self.assertIn("**Statement.** If P >= 2rho/3", text)
        self.assertNotIn("Markov", text)
        self.assertIn("proof omitted", text)
        full = self.run_tool("lemma", "L007", "--full")
        self.assertIn("Markov", full)
        self.assertNotIn("L006", full)

    def test_grep_reports_file_and_line_and_audits_shows_reports(self):
        text = self.run_tool("grep", "centroid")
        self.assertIn("PROVED.md:5:", text)
        self.assertIn("APPROACHES/A006-tree-packing-reduction.md:2:", text)
        self.assertIn("report.md", self.run_tool("audits"))
        (self.run / "audit-digest.md").write_text("# Digest\nA006: STOP x1\n")
        self.assertIn("A006: STOP x1", self.run_tool("audits"))

    def test_output_is_capped_with_a_notice(self):
        text = self.run_tool("--max-bytes", "1500", "node", "A006", "--section", "all", "--lines", "0")
        self.assertLess(len(text.encode()), 1700)
        self.assertIn("cut at 1500 bytes", text)


if __name__ == "__main__":
    unittest.main()


class RecordsValidationTests(RecordsToolTests):
    def test_validate_reports_structural_problems_and_passes_clean_records(self):
        text = self.run_tool("validate")
        self.assertIn("A001: index row without a node file", text)
        self.assertIn("A019: index row without a node file", text)
        self.assertIn("A006: unknown parent A002", text)
        self.assertIn("A006: broken link A002-rooted-flow-reductions.md", text)
        self.assertNotIn("A006: missing Abstract line", text)
        (self.run / "APPROACHES/index.md").write_text(INDEX.replace(
            "| [A001 — Verified benchmarks](A001-verified-benchmarks.md) | none | A004 | RESOLVED | Open problem noted. |\n", "").replace(
            "| [A019 — Dynamic trees](A019-dynamic-threshold-trees.md) | A006 | none | ACTIVE | Selection and total maintenance remain unproved. |\n", ""))
        (self.run / "APPROACHES/A006-tree-packing-reduction.md").write_text(NODE.replace(
            "Parents: [A002](A002-rooted-flow-reductions.md).", "Parents: none."))
        self.assertTrue(self.run_tool("validate").startswith("OK: 1 node files, 1 index rows"))

    def test_multiple_ids_and_verified_markers(self):
        text = self.run_tool("lemma", "L006", "L007")
        self.assertIn("## L006", text)
        self.assertIn("## L007", text)
        (self.run / "PROVED.md").write_text(PROVED.replace(
            "**Statement.** If P >= 2rho/3 then Pr(X=1) >= 1/2.\n",
            "**Statement.** If P >= 2rho/3 then Pr(X=1) >= 1/2.\n\n**Verified.** subagent 2026-09-18 — statement and proof\n"))
        overview = self.run_tool("overview")
        self.assertIn("L006 — A tree-connectivity promise reduces to small total flow input  [unverified]", overview)
        self.assertNotIn("L007 — Feasible arborescence packing exposes a minimum cut  [unverified]", overview)
        self.assertIn("Lemmas without a **Verified.** line: L006", overview)
        nodes = self.run_tool("node", "A006", "A999")
        self.assertIn("Abstract:", nodes)
        self.assertIn("No node file for A999", nodes)


class RecordsBriefTests(RecordsToolTests):
    def test_brief_bundles_the_lemma_with_cited_statements(self):
        (self.run / "PROVED.md").write_text(PROVED.replace(
            "**Proof.** Markov.", "**Proof.** Combine L006 with Markov's inequality."))
        text = self.run_tool("brief", "L007")
        self.assertIn("# Verification packet for L007", text)
        self.assertIn("Combine L006 with Markov's inequality.", text)
        self.assertIn("# Statements of cited lemmas (L006)", text)
        self.assertIn("**Statement.** Let T be any undirected tree.", text)
        self.assertNotIn("Remove r and recurse", text)
        self.assertIn("No lemma L999", self.run_tool("brief", "L999"))

    def test_overview_compacts_long_lemma_lists_unless_full(self):
        many = "# Proved lemmas\n\n" + "".join(
            f"## L{i:03d} — Lemma number {i}\n\n**Statement.** S{i}.\n\n**Proof.** P.\n\n" for i in range(1, 46))
        (self.run / "PROVED.md").write_text(many)
        text = self.run_tool("overview")
        self.assertIn("## Proved lemmas (45)", text)
        self.assertIn("IDs: L001, L002", text)
        self.assertNotIn("Lemma number 7", text)
        self.assertIn("Lemma number 7", self.run_tool("overview", "--full"))
