# Audit post-mortem: six reports, two batches, and why none of them broke the fixation

Paths: run = `/Users/yonggangjiang/Downloads/tcs-prover/runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global`; scratch = `/private/tmp/claude-503/-Users-yonggangjiang-Downloads-tcs-prover/62d437f4-1343-4f50-b460-a8439c134571/scratchpad`. All times UTC 2026-09-13. "B1-A1" = batch 1, audit 1, etc.

## 0. Mechanics (verified)

- Audit config for this run: `job-settings.json` lines 22–27: `intervalHours: 1.0`, models `[gpt-6-astra, claude-fable, kimi-k3]` (default in `workflows/research_audit.yaml` is 2h and `[none,none,none]`).
- Auditors run in parallel (`ui/audits.py:477` `ThreadPoolExecutor`), each "One fresh CLI session, with read-only tools and no delegated agents" (`ui/audits.py:149`, `-s read-only` at line 163). I found no per-auditor timeout in `run_auditor`; the auditors chose their own durations.
- Batch 1: started 17:11:58; reports at 17:22:33 (gpt, 11 min, 135 lines), 17:41:57 (kimi, 30 min, 113 lines), 17:49:22 (claude, 37 min, 120 lines). Author resumed 17:49:25. Workspace then: 32 nodes, 38 lemmas (B1-A3 line 5).
- Batch 2: started 19:49:23; reports at 20:00:31 (gpt, 11 min, 187 lines), 20:08:52 (claude, 19 min, 82 lines), 20:13:25 (kimi, 24 min, 145 lines). Author resumed 20:13:28. Workspace then: 45 nodes, 55 lemmas (B2-A2 line 5); PROVED.md "over 500 KB" (B2-A2 line 61).
- Author wall-clock lost to audits: 38 + 24 = 62 min of a ~7h run.
- Author prompt frames audits as advisory: `workflows/author_critic.yaml:130` "Consider their suggestions as advisory and decide whether to use them; they do not override the statement or these instructions."

## 1. Per-audit assessment

Classification key: (a) endorsed continuing the deepest branch (A019 dynamic-tree/certificate maintenance at batch 1; A019's A035–A045 certificate subtree at batch 2); (b) proposed a genuinely different approach; (c) anything resembling I4–I8 (single-tree update by circulation, polytope averaging, unbiased rounding, two-arborescence decomposition); (d) share of text on readability/format.

### B1-A1 gpt-6-astra (`audit_history/2026-09-13T172233Z-audit-1-gpt-6-astra-4b7fb27c.md`)
- Thesis, line 5: "The strongest next step is to resolve a total-work bottleneck, rather than continue testing additional fixed tree-sampling rules." Line 21: "L023 is currently the strongest positive route toward the full input model." Table line 15: what remains missing for L006–L007 is "Efficient construction of the distribution."
- Recommendations: (1) line 23 "Make A019's formal objective accommodate the compact representations its descendants actually develop"; (2) line 35 "Give A024 a concrete target for controlling changes in marginals" (the covariance identity and `V_marg` target, lines 39–63); (3) line 77 "Develop a direct terminal-flow alternative within the existing shared-search records" (Quanrud's terminal-cut uncrossing, lines 79–96).
- (a) Yes: item 1 is an interface refinement of A019; item 2 refines A024. (b) Item 3 is a cut-recursion direction, distinct from packing construction but already present as A013. (c) None. Item 2's "batch consecutive corrections to one arc" (lines 65–73) is still packing construction. (d) Lines 114–132 (~15% of lines, but 8 of 11 numbered bullets) are readability.
- Stop/step-back signal: none. Premise questioned: no; it restates the premise.

### B1-A3 kimi-k3 (`audit_history/2026-09-13T174157Z-audit-3-kimi-code-k3-6e3c6013.md`)
- Line 25: "The single bottleneck. Everything reduces to: construct (and sample from) a feasible fractional in-arborescence packing of mass > ρ/2". Line 46: "The bottleneck is exactly the packing construction, full stop."
- Recommendations (§5, lines 103–111): record the k-exit tradeoff `Pr[X ≤ κ] ≥ 1 − (ρ/P − 1)/κ`; "Formalize the κ ≥ 3 star hardness reduction (§3.2) as a new node"; record small-ρ/Gabow benchmark; "Open the JLSW-congestion study node"; "Time-box A019 ... re-scope A024 to a unit-capacity warm-up"; L008 sampling obstruction; CLNPS bottleneck; directed isolating cuts.
- (a) Partially: "time-box A019" (line 67) but keeps it as the frontier; A024 "stop expanding" (line 73). (b) The hardness node and JLSW-sharpening are different, but the JLSW item is still "construct a packing". The κ≥3 hardness reduction (lines 50–58) is mathematically wrong; the author refuted it in `APPROACHES/A013-shared-regions-promise-search.md:313–326` ("The proposed construction fails for every instance"). (c) None. (d) §4 lines 90–101 (~10%).
- Stop signal: yes, soft ("time-box", "mark BLOCKED with this analysis recorded"). Premise questioned: no; §3.1 argues the packing target is "provably minimal", which hardens the fixation on constructing it.

### B1-A2 claude (`audit_history/2026-09-13T174922Z-audit-2-claude-haiku-4-5-20251001--claude-fable-5-1-1ad9413a.md`)
- Line 7: "the research has stalled ... the last ten nodes are counterexamples to the author's own shortcuts or special cases, and neither frontier node has moved since L023. I recommend reviving the few-crossing search of A013 as the primary alternative, reframing A019 around subtree re-hanging, and parking the entropy branch." Line 25: "The work is circling."
- Directions: A (line 51) revive A013 with a k-crossing promise search fed by JLSW colors, lemmas P1–P3; B (line 65) reframe A019 as link-cut-tree subtree re-hanging (P4, Q1–Q3); C (line 77) regime table (ρ ≤ N^{o(1)} solved by Gabow, ρ ≥ √m by Chekuri–Quanrud); D (line 87) expanders. Line 89: "Do not open further nodes whose objective is to test one more fixed sampler, one more coupling rule, or one more static compression."
- (a) Partially: keep A019, reframed. (b) Yes (Direction A, C). (c) None, but two near-misses: Q3 (line 73) "Selection therefore needs a tree no worse than 1.5 times an average tree, not an approximately optimal tree. This weaker target may admit a local or randomized oracle"; and P4's "re-hang the subtree T_u through a new arc" is a single-tree-update view. Neither is developed toward averaging or rounding. (d) §3 lines 91–113 (~20%).
- Stop signal: yes ("Park A024", "What to stop doing"). Premise questioned: no; Direction A takes a packing (JLSW) as given.

### B2-A1 gpt-6-astra (`AUDITS/2026-09-13T200031Z-audit-1-gpt-6-astra-20908ab8.md`)
- Line 5: "The principal gap is still constructing and maintaining that packing in almost-linear total time."
- Main content, lines 25–104: a full proof that one min-cost circulation batches all prefix minimum-superset queries (checked on "722 prefix instances from 160 small random integer-capacity graphs", line 102); lines 106–123 "Narrow the certificate objective to the exits actually needed" for A019; lines 125–137 assessment of A045; line 143 a correction to A044 §4 Lemma 3.
- (a) Yes: continues both A034 and A019 with refinements. (b) No: a tool inside the split route. (c) No; it uses a circulation for prefix cuts, not for tree updates. (d) Lines 159–185 (~15%).
- Stop signal: weak ("lower priority" for the activity route, line 155; "further counterexamples ... have diminishing value"). Premise: no.

### B2-A2 claude (`AUDITS/2026-09-13T200852Z-audit-2-claude-haiku-4-5-20251001--claude-fable-5-1-05095af2.md`)
- Line 7: "the last eleven nodes (A035 through A045, lemmas L043 through L055) form a deep local-lemma cycle around the dual-certificate maintenance in A019. None of those lemmas touches the decisive gap". Line 23: L047's per-event cost times L023's event count "is worse than the trivial algorithm of 2(n−1) full flows by a factor of about m."
- R1 (line 32) record the Õ(ρN) small-value algorithm; R2 (line 34) large-side sampling; R3 (line 36) "Freeze the certificate subtree and state its missing theorem. Set A019 to BLOCKED ... New nodes under A019 should be admitted only if their objective is a whole-sequence bound"; R4 split-route progress mechanism and empirical testing; R5 literature checks; R6 deprioritize entropy; R7 (line 48) "the honest deliverable is likely a progress report."
- (a) No: the clearest STOP in all six. (b) Yes (R1, R2, R4). (c) None. Its route table (line 17) still states the gap as "A budget-meeting in-arborescence must be selected and maintained". (d) §3 lines 50–76 (~30%).
- Premise questioned: no.

### B2-A3 kimi-k3 (`AUDITS/2026-09-13T201325Z-audit-3-kimi-code-k3-d0251787.md`)
- Line 13: "Exactly one algorithmic premise is missing: constructing such a packing in N^{1+o(1)} time." Line 31, crux "(P) Construct, in N^{1+o(1)} time, a feasible fractional in-arborescence packing ... with a sampler — or replace it by a packing-free exact search." Line 18: the A021–A045 program "shows a real risk of an unproductive cycle"; line 52 "Recommendation: cap this line."
- New directions: §4.2 deep-read SXZ, Quanrud, CLNPS "as proofs, not theorems" with the packing-witness question; §4.3(a) trichotomy for random-source splits; §4.3(b) "The lattice of minimum sides" (wrong; refuted at `APPROACHES/A034-selecting-balanced-exact-splits.md:184–190`); §4.4 family ladder for A019; §4.6 two-exit relaxation table.
- (a) No (cap). (b) Yes (§4.2, §4.3). (c) None. The "packing-free" clause in (P) means the flow-only split route, not a sampler without a packing. (d) §§5–6 lines 110–133 (~25%).
- Premise: no; "full stop" language again.

### Summary table

| Audit | Endorsed deepest branch | Different approach | Any of I4–I8 | Readability share | Told author to stop/step back | Questioned "construct the packing" |
|---|---|---|---|---|---|---|
| B1-A1 gpt | yes | weak (Quanrud terminal flow) | no | ~15% | no | no |
| B1-A3 kimi | time-box | yes, one wrong (κ≥3 hardness) | no | ~10% | soft | no ("full stop") |
| B1-A2 claude | reframe | yes (revive A013, regimes) | no (two near-misses) | ~20% | yes (park A024, "what to stop doing") | no |
| B2-A1 gpt | yes | no (tool) | no | ~15% | weak | no |
| B2-A2 claude | no: "Set A019 to BLOCKED" | yes (R1, R2, R4) | no | ~30% | yes, strongest | no |
| B2-A3 kimi | no: "cap this line" | yes, one wrong (lattice) | no | ~25% | yes | no |

## 2. Stop signals and the premise

- Stop signals existed: B1-A2 line 89, B1-A3 line 67, B2-A2 line 36 ("Set A019 to BLOCKED"), B2-A3 line 52 ("cap this line"), line 143 ("the next node that merely adds another sufficient interface should be treated as a signal to stop"). Between batch 1 and batch 2 the author nevertheless created A035 (18:29), A037 (18:43), A038 (18:49), A039 (19:10), A042 (19:22), A045 (19:48) and their siblings, i.e. the certificate subtree that B2-A2/B2-A3 then called a cycle (`filechanges.txt` first-touch times). After batch 2, A019 stayed ACTIVE (`APPROACHES/index.md` A019 row) although no new A019 descendants appeared; the "freeze" happened de facto, not as a recorded decision.
- Not one auditor questioned the premise. All six restate "construct/maintain a packing" as the single missing primitive (B1-A1:15, B1-A3:25 and 46, B1-A2:7, B2-A1:5, B2-A2:17, B2-A3:13 and 31). The PDF's step I3/I4 is precisely the negation of this premise: L019 (existence) is enough, provided one iterates a single tree with a feasibility-certified circulation. The closest hints were B1-A2 Q3 ("a tree no worse than 1.5 times an average tree, not an approximately optimal tree") and B1-A2 P4 (single-tree re-hang), neither developed. B1-A3 §3.1's "provably minimal" argument and B2-A3's "essentially equivalent in difficulty to the original problem" (line 17) actively reinforced the belief that the packing must be built.

## 3. What the author did with each batch

### Batch 1 (17:49–18:20; `narrative.txt` 4110–4735)
- 17:49:34–17:49:41 THINK "Noticing audit data", "Managing token limits for reports" (line 4123–4124): the three reports (~55 KB) strained the root context. Root compaction followed at 17:56:51 (line 4251), seven minutes after reading them; after that the root said only "I'll reread the saved instructions and current audits" (17:56:57). I infer the root never held all three reports in context at once; subagents consumed them piecemeal.
- 17:51:40 root summary (line 4134): "I've read all three audits. Their central recommendation is to focus on the remaining total-work bounds, and I'll follow that direction. I'll also verify their proposed lemmas before adopting them—especially the claimed hardness reduction". This compresses three divergent reports into the reading most compatible with continuing A019 ("total-work bounds" = A019's amortization gap).
- Adopted: B1-A2 P4 evert interface and B1-A3 phase-count warning, recorded in `A019-dynamic-threshold-trees.md:151, 194, 198, 281` (subagent result 17:59:57: "A019 now supports both explicit and compact implementations ... proves the whole-run Θ(k log k) phase bound"); B1-A1 item 2 → L039 in A033 (18:01:44, `A033:26` "Audit-1 in the current September 2026 batch suggested the variation target"); B1-A1 item 3 and B1-A2 P1–P3 → A013 revived, L040/L041 (18:04:53 "Verified Quanrud §§3.1–3.2 directly ... Corrected P1–P3 and rejection of the invalid triangle reduction"); B1-A3 κ-tradeoff → added to L007 with a correction of kimi's conclusion (`A006:51–55`); B1-A2's JLSW caveat → A001 (`A001:79` "audit-2's caveat is accurate. An unconditional invocation ... as in parts of audit-3, is not justified").
- Rejected with proof: B1-A3 κ≥3 hardness (`A013:313–326`).
- Ignored: B1-A3 "time-box A019" and B1-A2 "do not open further nodes whose objective is ... one more static compression": 11 A019-descendant nodes and L043–L055 followed in the next 90 min (root messages 18:26–19:48, lines 4902–6834, all about certificate monitoring, repair, potentials). B1-A2 Direction C regime table and B1-A3 small-ρ benchmark: never promoted (index line 79: "The exact low-value and large-optimum-side regimes suggested in the current audits have not yet been promoted as standalone algorithmic lemmas"). B1-A3 §3.5.4–3.5.6 (L008 sampling obstruction, CLNPS bottleneck, isolating cuts): no node exists.
- Net: batch 1's one strategic success was reviving A013 → A034/A036 (split route). Its clearest strategic warning (stop refining A019 interfaces) was inverted into a mandate to build more A019 machinery.

### Batch 2 (20:13–20:45; `narrative.txt` 6854–7553)
- 20:14:58 root (line 6888): "Its strongest proposal is a way to compute an entire chain of minimum-superset cuts with one minimum-cost flow. I'm checking that reduction ... I'm also correcting A045's stale index status and bringing A044's required assumption about graph updates forward". Three subagents launched at 20:14:29–20:14:38 (`batch_supersets`, `audit_cleanup`, `approximation_proofs`).
- Adopted: B2-A1 batched chain → A046/L057 RESOLVED by 20:20:25, then A047/L058–L059 (20:33), A048/L060, A049/L061, A050/L062; B2-A1 A044 Lemma 3 qualification (20:16:58, edits at A044 lines 26, 105–109, 113, 171, 181–197, 252); A045 index status fix; B2-A3 §4.2(b) packing-witness check → A001 (20:18:36 "Neither inspected approximation proof constructs an efficiently represented fractional arborescence packing"); B2-A3 §4.2(a) SXZ deep read (20:41:58–20:44:56 "Length increases in L023 do not preserve that setting"); B2-A2 R4 "test on adversarial families" → the L059/L060/L062 lower bounds.
- Rejected with proof: B2-A3 lattice (`A034:184–190`). Index line 81: "Their advice is not treated as a proof: global rooted minimum sides need not form a lattice, and a locally expensive implementation does not establish a lower bound on every maintenance strategy" (the second clause rejects B2-A2's cycle-family test as a lower bound).
- Ignored: R3 status change (A019 still ACTIVE); R1/R2 (index line 79); R7 progress-report deliverable; B2-A2 R6 and B2-A3 §3.2 "deprioritize entropy": from 20:58 (line ~7600 onward, root message "I'm testing a different use of the tree reduction: undirected tree samples") the author opened A051–A060 and L063–L073, an entropy/Laplacian variant that no auditor suggested and two auditors advised against.
- Net: batch 2 was consumed as a to-do list of local lemmas (L056–L062 within 30 min), not as a strategic reset.

## 4. Structural critique of the audit design

1. Prompt content (`workflows/research_audit.yaml:39–56`). Goal 1 is three sentences; Goal 2 (readability, "define every term and notation", "identify places in a file where the reader may be confused") is longer and more concrete, so every report spends 10–30% on links, anchors, duplicated paragraphs and notation tables. Goal 1 asks to "critically examine the author's approaches" but never asks the auditor to (i) solve the problem from the statement before reading APPROACHES/, (ii) enumerate the author's unexamined premises, (iii) identify lemmas used only existentially and ask whether a constructive version is needed, (iv) specify for each ACTIVE node the weakest object its consumer actually needs. Point (iv) is exactly where the PDF diverges: L007 consumes marginals of a random tree; A019 builds a full packing.
2. Anchoring. "Form your own assessment of the research records" makes the records the object of study; all six auditors adopted A006's framing within their first page. Two independent models writing "full stop" and "essentially equivalent in difficulty" is evidence the author's framing is contagious, not that it is right.
3. No synthesis and no arbitration. Three 13–25 KB reports with conflicting priorities (B1-A1 "L023 is the strongest positive route" vs B1-A3 "time-box A019" vs B1-A2 "reframe A019") were handed to an author that had to compress them under token pressure (17:49:41) and then lost them to compaction (17:56:51). The author's own one-line synthesis ("focus on total-work bounds") selected the continuation-friendly reading. Advice that conflicts with the author's momentum has no champion after the auditors exit.
4. Advisory-only status with no required response. The author never had to write "adopt/reject with reason" per recommendation; responses are scattered (`A006:55`, `A013:438`, `A034:196`, index 79–81). "Set A019 to BLOCKED" could be ignored silently. The harness's only enforcement is the resume line "read them and consider their advice" (`author_critic.yaml:175`).
5. Errors in audits poison trust. Two of six reports contained false theorems (κ≥3 hardness, min-side lattice). The author spent subagent time refuting them and then wrote "Their advice is not treated as a proof" (index line 81), a rational discount that also covered the correct strategic advice. Auditors were told to "Propose new approaches" but not to label claims as verified/conjectured, or to run a check before asserting a theorem.
6. Frequency and scope. Configured at 1 h, batches landed at 2h05 and 4h43 of author time (turn boundaries and a 51-min final turn). Between batches the workspace grew from 32 to 45 nodes and PROVED.md to >500 KB; gpt spent 11 minutes on each batch, kimi 24–30, claude 19–37. The audits were reviewing a moving target far larger than could be read; auditors that claim to have "read all 38 lemmas" in 30 minutes (B1-A3 line 13) are extrapolating. Pausing the author for 62 minutes bought two reviews that mostly reproduced the author's own gap analysis.
7. Read-only, no subagents, no experiments by default. B2-A1 did run an exhaustive check (line 102) and its proposal was the only one adopted as a lemma within 10 minutes; the two wrong proposals came from reports that ran nothing. Requiring a computational sanity check for any proposed theorem would have removed both errors.
8. What would have caught the fixation (inference, not verified): an auditor instructed to first write its own 1-page plan from the statement, then to answer "which lemma does the author use only for existence, and what is the weakest object L006/L007 need?" would have had L019 + L007 in front of it and the question "can you sample a tree with marginals ≤ (3/2)c/λ without representing the packing?" is a short step from there. A second lever is a hard rule: a node whose last k descendants all end with "total-work bound remains open" must be marked BLOCKED by the auditor's verdict unless the author writes a one-paragraph rebuttal; B2-A2 asked for this and was ignored.

## 5. Concrete rewrite of the audit prompt

Replace the `prompt:` block of `workflows/research_audit.yaml` with the following (Goal 2 demoted to an appendix and capped):

```
You are an independent research adviser reviewing a paused research run. You have
read-only tools in the author's workspace. Do not edit files, do not delegate, and do
not read AUDITS/ or audit_history/.

PHASE 0 — Independent plan (do this BEFORE opening APPROACHES/ or PROVED.md).
Read INITIAL_PROMPT.md only. Spend real effort on the statement itself: write a
one-page plan of how you would attack it today, listing the two or three standard
theorems you would lean on and, for each, whether you would use it constructively or
only existentially. Save this plan verbatim as Section 1 of your report. It is your
anchor against adopting the author's framing.

PHASE 1 — Read the records: APPROACHES/index.md first, then every ACTIVE node, then the
lemmas those nodes cite. Skim the rest. Report which files you actually read in full.

PHASE 2 — Premise audit. Answer, with lemma IDs:
  (a) For each ACTIVE node, what is the WEAKEST object its downstream consumer needs
      (e.g. "L007 only needs arc marginals of one random tree"), and what STRONGER
      object is the node building? Name every gap between the two.
  (b) Which lemmas in PROVED.md are existence results that the author is trying to
      make constructive? For each, state whether the final algorithm needs the
      construction, or whether existence plus a feasibility/LP/flow argument suffices.
  (c) State the author's top three unexamined assumptions in one sentence each, and
      how your Phase-0 plan differs from the author's on each.
  (d) Compare your Phase-0 plan with the DAG: which of your ingredients has no node?

PHASE 3 — Trajectory verdict. For every ACTIVE node give exactly one of:
  CONTINUE (state the single theorem that would finish it and why it is reachable),
  STOP (state the evidence: e.g. k consecutive descendants ending in "total bound
  remains open", or a lower bound the author already proved), or
  REDIRECT (name the replacement objective).
  A node with STOP must be listed in a "Recommended status changes" table.
  Do not recommend CONTINUE for a node whose last three descendants added only
  sufficient interfaces, static tools, or counterexamples to the author's own
  proposals, unless you can name the missing amortization argument concretely.

PHASE 4 — New directions. Propose at most three, ordered by expected value. Each must
say (i) which Phase-2 gap it exploits, (ii) the first lemma to prove, (iii) a small
sanity check you ran (enumeration, random instances) if you claim any mathematical
statement. Label every mathematical claim VERIFIED (you checked it), CHECKED-SMALL
(computationally on small cases), or CONJECTURE. Never present a conjecture as a
theorem; a wrong "theorem" costs the author more than no advice.

PHASE 5 — Consistency and readability (at most 15% of the report). Only: stale
statuses, index rows that contradict node files, broken or anchorless links,
undefined first uses in ACTIVE nodes. Do not propose structural changes.

FORMAT — Begin with a 10-line executive summary: verdicts per ACTIVE node, the
top premise gap, the one direction you would pursue first. Keep the whole report
under 2,500 words; the author reads it under a token budget and after context
compaction, so put decisions first and proofs last.
```

Companion changes to the author side (`author_critic.yaml` resume prompt), since advice without a response protocol is discarded: "For each auditor's status verdict and each numbered direction, append to APPROACHES/index.md one line: ADOPTED / REJECTED (with the refuting lemma or a one-sentence reason) / DEFERRED. A STOP verdict from two or more auditors on the same node sets its status to BLOCKED until you record a rebuttal in that node." And in the controller: compute one merged summary (per-node verdict tally and the union of proposed directions) before resuming the author, so three parallel reports are not compressed by the author into the most convenient reading.


---

## Key findings

- [verified] None of the six auditors questioned the premise 'construct the packing'; all six restate constructing/maintaining a feasible packing as the single missing primitive, and two (kimi) reinforce it with 'full stop' and 'essentially equivalent in difficulty to the original problem'.
  - evidence: B1-A1 line 15; B1-A3 lines 25, 46; B1-A2 line 7; B2-A1 line 5; B2-A2 line 17; B2-A3 lines 13, 17, 31 (files under audit_history/ and AUDITS/).
- [verified] No audit proposed anything resembling PDF ingredients I4–I8 (single-tree circulation update, polytope averaging, unbiased rounding, two-arborescence decomposition). The closest hints were B1-A2's Q3 ('a tree no worse than 1.5 times an average tree') and P4 (single-tree re-hang), neither developed.
  - evidence: B1-A2 lines 67–73; grep for circulation/rounding/Edmonds across all six reports finds only the prefix-cut circulation of B2-A1 lines 25–104 and the closed A010–A018 branch.
- [verified] Explicit stop signals were given (B1-A2 line 89, B1-A3 line 67, B2-A2 line 36 'Set A019 to BLOCKED', B2-A3 line 52 'cap this line') and were not followed: 11 A019-descendant nodes (A035–A045) and L043–L055 were created in the 90 minutes after batch 1, and A019 remained ACTIVE in the final index.
  - evidence: filechanges.txt first-touch times A035 18:29, A037 18:43, A038 18:49, A039 19:10, A042 19:22, A045 19:48; APPROACHES/index.md A019 row status ACTIVE; narrative.txt root messages 18:26–19:48.
- [verified] The author compressed batch 1 into 'focus on the remaining total-work bounds' (17:51:40), a reading that favored continuing A019; the root hit token limits reading the reports (17:49:41) and its context compacted seven minutes later (17:56:51).
  - evidence: narrative.txt lines 4123–4124, 4134, 4251.
- [verified] Batch 1's one strategic success was reviving A013 into the split route (L040/L041 → A034/A036); batch 2 was consumed as a lemma to-do list (L056–L062 within ~30 minutes) plus the batched-chain proposal (A046/L057), while R1/R2 regime lemmas, R3 BLOCKED status, R7 progress-report deliverable, and 'deprioritize entropy' were ignored (author opened entropy variant A051–A060 from 20:58).
  - evidence: narrative.txt 18:04:53, 18:15:38, 20:14:58, 20:20:25, 20:33:21, 20:58:22, 21:04:19; index.md lines 79, 81; index rows A046–A060.
- [verified] Two of six reports asserted false theorems (kimi's κ≥3 star hardness reduction; kimi's lattice of minimum sides), which the author refuted and then used to discount audits generally ('Their advice is not treated as a proof').
  - evidence: B1-A3 lines 48–58 refuted at A013-shared-regions-promise-search.md:313–326; B2-A3 §4.3(b) refuted at A034-selecting-balanced-exact-splits.md:184–190; index.md line 81.
- [verified] Audit mechanics: interval configured 1.0h with gpt-6-astra/claude-fable/kimi-k3 in parallel, read-only, no subagents, no per-auditor timeout; auditors spent 11–37 minutes on 500KB+ of records; the author was paused 62 minutes total; the readability goal is the longer half of the prompt and consumed 10–30% of each report.
  - evidence: job-settings.json lines 22–27; ui/audits.py lines 149, 163, 477; report timestamps 17:11:58→17:22/17:41/17:49 and 19:49:23→20:00/20:08/20:13; research_audit.yaml lines 39–56.
- [speculative] An audit prompt requiring (i) an independent plan before reading the records, (ii) a per-ACTIVE-node 'weakest object the consumer needs vs what the node builds' analysis, (iii) identification of lemmas used only existentially, and (iv) per-node STOP/CONTINUE verdicts with a mandatory author response would have had a real chance of surfacing the L019-existence-suffices insight; whether a given model would then find the circulation/rounding sampler is not verifiable from this run.
  - evidence: Inference from the fact that all six auditors anchored on A006's framing within their first page and none examined L019's existential use; the prompt contains no instruction of this kind (research_audit.yaml lines 39–56).
