# Post-mortem: why the 2026-09-13 directed min-cut run missed the arborescence-sampling solution, and what to change

Run analysed: `runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global/` (gpt-6-astra, effort ultra, Codex multi-agent, hourly audits by gpt-6-astra / claude-fable / kimi-k3).
Reference solution: `Exact_Directed_Min_Cut.pdf` ("Sampling Arborescences for Exact Directed Minimum Cut", draft of 2026-09-07).
Method: the 65 MB transcript was flattened into a time-ordered narrative; 19 specialist analyses (PDF soundness, 7 time windows, 6 approach clusters, audits, prompts, runtime, 2 counterfactuals) were synthesised and then attacked by 3 adversarial verifiers; the corrected synthesis is `appendix-A-verified-synthesis.md`, the verifier objections are `appendix-B-verifier-objections.md`, and the raw analyses are in `specialist-reports/`. Every claim below was checked against the primary files by at least one of those passes; timestamps are UTC on 2026-09-13.

---

## 1. Summary

1. **The run found the outer shell of the solution in 27 minutes and never touched its core.** By 15:13 the records held the same reduction the PDF uses (L001 rooted formulation, L006 centroid recovery, L007 "one crossing with probability ≥ 1/2 from a packing of mass ≥ 2ρ/3"), and by 15:33 the packing-existence theorem L019. The PDF's only new content is the sampler (ingredients I4–I8 below: keep one tree, move it halfway toward the target marginals by one circulation on an exchange network, round unbiasedly, split into two arborescences). None of I4, I6, I7, I8 appears anywhere in 60 nodes, 73 lemmas, 209 web searches, or 10,000 lines of reasoning summaries.
2. **Every route the author took treated the sampling distribution as a fixed object to be constructed and represented** (a packing plus sampler in A006; MWU-weighted packings A008–A019; product / max-entropy laws A022–A033 and A051–A060) and then hit the cost of computing that object's marginals. The PDF never represents the law: its only "marginal" is the indicator of the current tree, and the target marginals are bounded by induction over a random process. The author wrote the marginal target down twice (L028 at 15:59, L063 at 21:04) and both times went looking for a distribution rather than a process.
3. **The author already owned the PDF's machinery in other roles.** L034/A028 is the PDF's exchange network arc for arc (mirrored), compressed even better (O(m + n log n)), but labelled with costs for negative-cycle search inside a dynamic-tree oracle; its own caveat, "does not assert that every negative cycle describes a valid simultaneous tree exchange", is exactly the objection the PDF's half-step lemma dissolves by averaging with the current tree. L013 is the lower-bound-circulation feasibility argument, applied to a known cut on the input graph. L063 §2 states the arborescence-polytope inequality x(E(S)) ≤ |S|−1, used only for an existence equivalence. Unbiased flow rounding and Edmonds' disjoint-branching theorem were never searched or cited.
4. **The process locked the framing in place.** 35 of 60 nodes are RESOLVED yet none closes the missing step; 16 of 73 lemmas are lower bounds against the author's own sub-proposals; progress was reported as node and lemma counts; 9 root compactions each triggered an index-first recovery ritual that re-read A006's "construct a packing" frame; the harness's continuation prompt was never delivered (turns were reopened by the Codex goal loop); INITIAL_PROMPT.md was edited twice during the run and one steer concerned the index; 97 of 125 subagent results were verification or formatting.
5. **The six audits reinforced the premise instead of questioning it.** All six restate "construct/maintain a packing" as the missing primitive (one says "full stop"); two told the author to keep the circulation branch closed and rated A028 low value, i.e. the two objects the PDF reuses; one auditor asserted two false theorems; stop signals were advisory and were not followed (A019 stayed ACTIVE through 11 descendant nodes).
6. **The run did not run out of time.** It died at 5.8 hours of a 168-hour budget on a Codex usage-limit error during a compaction, with the last subagent result (an Anari-style single-tree exchange walk, the closest the run ever came to I4, aimed at a fixed product law) unsaved. Resuming in the same regime would not obviously converge.
7. **The earlier 10-hour run (Sep 12, 75 nodes, 329 lemmas) failed the same way.** It wrote the marginal formulation explicitly (A036: "Suppose E|δ_T(U*)| ≤ 2−ε") and then built a generative packing with polylogarithmic congestion via cut-matching (L290), which cannot give exactness. The failure mode is repeatable, so it is a harness problem, not bad luck.
8. **The PDF is sound as far as we can check.** Lemmas 2.2, 3.1, 4.1, 5.1, 6.1, 6.2, 7.1, Theorem 1.1 and Appendix A were re-derived with no gap, modulo the cited results (Chen et al. maxflow, Tarjan 1976 disjoint branchings, Kang–Payor rounding). Two trivial omissions: the global-to-rooted reduction is left implicit, and the repetition count must be R = ⌈(a+3) log₂(n+m)⌉ to meet 1 − (n+m)⁻³ when m is not polynomial in n.

Section 6 gives concrete prompt text and runtime changes. They are ranked by how directly they address the documented failure; the run contains no experiment proving any of them works, and the audits (six fresh threads of three models that did see the statement) are a partial counter-experiment that failed the same way. Section 7 proposes a cheap pilot.

---

## 2. What the solution needs versus what the author had

The PDF works with in-cuts δ⁻(S) and out-arborescences from a root; the author used the mirror (out-cuts, in-arborescences). Everything below is up to that reversal.

| # | PDF ingredient | Author's closest artifact | Gap |
|---|---|---|---|
| I1 | Centroid recovery from a one-respecting tree, O(log n) maxflows (Cen et al.) | L006 (15:13), batched as ≤ n−1 contracted instances of total size O(N log n) | none |
| I2 | It suffices that one random arborescence has E\|T ∩ δ(S*)\| < 3/2 | L007 (15:13): Pr[one crossing] ≥ 2 − ρ/P; L028 (15:59): "distribution on trees with marginals ≤ c_e/P"; L063 (21:04): any undirected-tree law with a degree-deficit term | the target existed, always as a property of a fixed law whose marginals are to be computed |
| I3 | ∃ x in the arborescence polytope with x ≤ c/λ (Edmonds), used only existentially | L019 (15:25–15:33, LP duality), tagged "no running-time or small-support representation bound follows" | used as a certificate only inside potentials and the MWU oracle budget, never for the feasibility of a tree-update flow |
| I4 | Keep ONE tree; replace it by a random T′ with E[1_{T′} \| T] ≤ (1_T + q)/2; O(log n) steps | absent. Nearest: L023's deterministic single tree under a cost budget; a 15:38 acyclic-case sampler with marginals ≤ c(e)/D stopped by "cyclic dependence"; the 22:05:43 Anari/CGM exchange walk for a fixed product law | no process with a linear conditional-expectation invariant |
| I5 | Circulation on the exchange network with bounds [1−q_e, 1] on tree arcs, [0, q_f] on non-tree arcs; feasibility from I3 via Hall on fundamental paths | L013 (15:20): lower-bound circulation completed by one maxflow, on G, for a known cut. L034/A028 (16:32): the exchange transitions e→f on fundamental paths and same-tail f_u→e, one node per arc, O(m + n log n). A055 §1–2 (21:2x): one-maxflow Hall test for a law's degree deficits on G | right primitive, wrong network, wrong labels (costs not bounds), wrong object (a law's marginals, not 1_T), wrong role (certificate, not update). Circulation branch BLOCKED by 16:05; audits: "do not reopen", "A028 low value" |
| I6 | Half-step lemma: (1_T + F)/2 is in the arborescence polytope even if F is cyclic | absent; the objection it answers is recorded verbatim in L034's caveat and A028:32 | the two-case argument (T[S] connected / disconnected) has no analogue in the records |
| I7 | Unbiased (expectation-preserving) rounding of the circulation (Kang–Payor, or Euler-tour parity for dyadic values) | absent; "rounding" occurs 40 times in PROVED.md, all numerical precision; A046:301 cites deterministic integral rounding (van den Brand et al.) | never searched or cited |
| I8 | T + B has every root-excluding cut ≥ 2, so Edmonds/Tarjan split it into two arborescences; pick one uniformly | absent; arc-disjoint pairs appear only as ρ = 2 certificates inside counterexamples | Edmonds' disjoint-branching theorem never cited |
| I9 | Heavy-light + segment-tree compression of fundamental paths, O(m log² n) | L034: binary-lifting gadget, O(m + n log n), preserves walk weights; A041/A043 use HLD + segment trees for load bookkeeping | whether L034's gadget works as a flow gadget (PDF Lemma 5.1) was never asked |
| I10 | Geometric guesses of λ, dyadic q, every candidate a valid cut | A024, A006, L071 | same mechanism; the target vector q never defined |
| I11 | Polylog exact maxflows on O(m log² n) networks | A006 (van den Brand et al. primitive), recovery accounting | the sampler's flow calls do not exist because the sampler does not exist |

**The minimal chain from the author's 17:00 state to the PDF** (counterfactual analysis) is five steps, each a reformulation rather than a new theorem: (1) restate the missing primitive as "one random tree with marginals ≤ (3/2)c/λ", which L007's proof already uses; (2) use L019 only as a feasibility certificate; (3) iterate one tree with E[1_{T′} | T] ≤ (1_T + q)/2; (4) compute the fractional step as a bounded circulation on L034's exchange graph and prove (1_T + F)/2 is in the polytope; (5) round unbiasedly and split T + B by Edmonds/Tarjan. Steps 1–2 are cognitive moves the author never made; steps 4–5 are textbook lookups it never ran. The bottleneck is step 3: nobody (author, subagent, auditor) ever proposed improving a single maintained sample toward target marginals.

---

## 3. Timeline of the run and its missed forks

| Time | Event |
|---|---|
| 15:06 | Fresh run starts. Three subagents spawned. |
| 15:07:50 | literature_status finds an Aug 2026 paper calling the problem "a major open problem". Recorded as A001 (RESOLVED). The 15:16 turn-end message opens "I did not resolve the statement. An August 2026 primary source…"; at 18:48 the root privately judges the task "practically impossible here… possibly open research" and keeps adding bookkeeping lemmas. |
| 15:10:37 | flow_reduction (3 min 54 s after spawn) delivers the full conditional reduction, naming the gap as "the missing near-linear arborescence-packing algorithm". A006:71 fixes the premise as "a sampler for a feasible packing… independent in-arborescences from that packing". **Fork 1**: the process (one tree, contracting expectation) was never named as an alternative to the object. |
| 15:17–15:22 | Subagent title "Exploring Markov chains for arborescence sampling" leaves no trace; the idea is realised as the fixed product law A007, killed by L009 (exp(−√n) one-crossing probability). **Fork 2**: adaptive single-tree chain never formulated. |
| 15:20–15:33 | L013 (lower-bound circulation feasibility) and L019 (packing existence) proved; both filed as non-algorithmic for the packing. **Fork 3**: "which auxiliary circulation does L019 certify feasible?" never asked. |
| 15:38–15:55 | structural_approaches finds a one-tree sampler with marginals ≤ c(e)/D for acyclic graphs, obstruction "cyclic dependence"; answered with SCC factorisation (A020). |
| 15:51:11–15:51:35 | Root reasoning titles "arborescence union and matroid partition", "basis polytopes and arborescences", "sampler limitations"; 3 s later max-entropy is delegated; nothing written. **Fork 4**: the polytope view (I0/I6's language) lost at the next compaction. |
| 15:46, 16:32 | First two root compactions; the 16:39 user steer ("You missed several new added node in the approach index.md") comes 7 minutes after the second. INITIAL_PROMPT.md changed externally at ~15:27 and ~16:02; ~25 minutes of reformatting all nodes. |
| 16:32–16:43 | A028/L034: the exchange graph, compressed, with cost labels, caveat "does not assert that every negative cycle describes a valid simultaneous tree exchange". **Fork 5**: relabel with bounds, compute one circulation, average with 1_T. |
| 17:11–17:49 | Audit batch 1 (author paused 38 min). Kimi: "the packing construction, full stop", "circulation branch conclusively closed… do not reopen". Claude: "Why A025 and A028 are low value", and the closest hint (Q3: "a local or randomized oracle" for a 1.5×-average selection target). |
| 17:49–19:49 | Author adopts "focus on total-work bounds"; opens the certificate-repair chain A034–A045 (10 nodes, ~520 KB, 89 min) whose own text says it does not close the gap. Compactions at 17:56, 18:31, 19:08, 19:42. |
| 19:49–20:13 | Audit batch 2 (author paused 24 min). "Set A019 to BLOCKED", "deprioritise the entropy route", one auditor asserts a false lattice claim. A019/A024/A033 remain ACTIVE. |
| 20:14–20:56 | Audit-suggested batched-superset recursion A046–A050 with three lower bounds (L059, L060, L062) against the author's own orderings; abandoned. |
| 20:58–21:04 | L063: any undirected-tree distribution with capacity-feasible marginals and balanced mean degrees suffices. Root title "polymatroid intersections and arborescence polytopes"; 7 s later a web search for "maximum entropy spanning tree distribution prescribed marginals". **Fork 6**: distribution instead of process. |
| 21:04–22:06 | Laplacian-Hessian entropy duals, Gaussian sketch lower bounds (L065, L069), parameter-range lower bounds (L066, L070), FISTA constants; A055 grows to 82 KB. Root title "matroid and flow rounding techniques" at 21:26:10 leaves no trace. |
| 22:05:43 | batch_supersets reports an Anari et al. single-tree exchange walk with link-cut trees, as a sampler for an optimised product law. |
| 22:06:37 | "You've hit your usage limit… try again at Sep 20th" during a root compaction. Elapsed author time 20,829 s of 604,800 s. |

---

## 4. Root causes

### Conceptual

**C1. Object versus process.** The sampling law was always a fixed, explicitly parametrised object (packing weights, product activities, entropy duals) to be constructed, whose marginals must then be computed or detected. The PDF's law is implicit in a process and never represented. Evidence: A006:71, index.md:68, L028, L063, A024's "marginal-detection oracle", A055:660 ("access to one maximum-flow test after exact marginals are supplied is not access to those marginals"), the 20:58:13 search for a max-entropy law with prescribed marginals, the 22:05:43 exchange walk for a fixed law. Interaction: the audits hardened it ("provably minimal", "full stop"); compaction recovery re-read A006's frame every time.

**C2. Existence theorems applied as certificates only to the wrong auxiliary problem.** The PDF uses L019's content once, to certify that a bounded circulation on the exchange network is feasible for every current tree. The author had the certificate technique (L013), the network (L034) and the existence theorem (L019) and never combined them; L019 was tagged "gives no efficient representation or construction". Interaction with the audits: "do not reopen" the circulation branch on the strength of L025, which concerns circulation-as-cut-extractor, not circulation-as-tree-update.

**C3. Literature anchoring.** (a) The "major open problem" finding at minute 2 set a progress-report mode for the whole run. (b) Technique lookups (decremental SCC, directed Laplacian solvers, Hessian sparsification, MCMC samplers; 25 of 209 searches on one decremental-graphs paper) were each followed by nodes built on the imported machinery, some of which became lower bounds against that machinery. (c) Zero searches for Edmonds' disjoint branchings, flow rounding, or the arborescence polytope.

**C4. Obstruction loop.** Propose a rule, build an Ω(·) family, promote a lemma, propose the next rule: L059/L060/L062 against three orderings of one recursion; L065/L066/L069/L070 against the author's own estimators; 16 of 73 lemma titles are negative results, 12 of 60 node titles. Each ends with a scope sentence, so no negative result propagated up to C1 or C2, and each was cross-verified and counted as progress.

**C5. Toolkit gap.** Even with C1–C2 fixed, integralising a fractional half-step needs unbiased rounding (I7) and a decomposition theorem (I8). The author knew Edmonds only in the optimum-branching / laminar-dual form and rounding only as numerical precision.

### Process

**P1. Record-keeping load, RESOLVED inflation, progress by count.** RESOLVED = "local objective achieved" gave every tool and every self-refutation a RESOLVED node (35 of 60). Turn-end messages measured progress in counts ("50 indexed approaches and 62 proved lemmas"). The self-contained-writing rule produced 1.4 MB of node files (A001 and A055 above 80 KB each) and 900 KB of PROVED.md, most of it duplicated background. 169 commands re-read INITIAL_PROMPT.md; 31 of 88 root messages report index state. Two external edits of INITIAL_PROMPT.md during the run cost ~25 minutes of reformatting.

**P2. Compaction churn and loss of unrecorded reasoning.** 9 root compactions (plus 21 in subagents, never re-anchored by the harness) at 165–230k tokens of a 258k window (`model_auto_compact_token_limit_scope="body_after_prefix"`). Codex stores only reasoning-summary titles, so every root idea that was not delegated or written (the polytope view at 15:51, 20:58, 21:13; flow rounding at 21:26) vanished. Recovery ritual: INITIAL_PROMPT → index → audits → node files → same node.

**P3. Subagents used for verification and formatting, never as independent solvers.** Of 125 subagent results: ~50 verification of sibling records, ~47 writing/formatting/migration, 8 literature checks, ~20 new mathematics (most in the first 40 minutes of open-ended mandates). No subagent was ever given only the statement; all six shared the workspace and were the same model.

**P4. Audits reinforced the premise and had no teeth.** All six restate the packing premise; none examined L019's existential use; two closed the branches the PDF reuses; one asserted two false theorems, after which the author wrote "Their advice is not treated as a proof". The audit prompt already asks to "diversify approaches" and detect "circulating in a non-promising cycle"; it does not ask about premises, weakest sufficient objects, or existential use. Half the prompt is about readability. The author was paused 62 minutes for two batches.

**P5. The continuation prompt was never delivered.** `render("continuation")` fires only when a turn ends blocked/complete; every in-segment turn end was reopened by the Codex goal loop with the two-sentence goal objective, whose only concrete verb is record maintenance. Nothing ever told the model to re-derive its plan.

**P6. Usage-limit death at 3.4% of budget.** Four gpt-6-astra threads at 150–230k context, ~268M input tokens (98% cached) and ~1.8M output tokens in 5.8 h, plus a gpt-6-astra auditor on the same quota; no alarm; the last result unsaved.

---

## 5. What to keep

- Parallel subagents found the complete reduction in under four minutes, fully proved.
- The records are rigorous: no false lemma was found by any analyst; obstruction lemmas state honestly what they do not exclude; the two false claims in the run came from an auditor.
- The author detected both external prompt edits by hash check within minutes and kept the DAG reciprocal.
- L034 (exchange compression) and L063 (marginal criterion for undirected-tree laws) are reusable.
- The DAG + PROVED.md layout made a line-by-line post-mortem possible. Keep the structure; cut the volume.
- Read-only, parallel, multi-model audits are the right design; the prompt and the lack of enforcement are what failed.

---

## 6. Improvements, ranked by expected impact per unit cost

### 6.1 Author prompt: plan-first, weakest-object discipline (targets C1, C2, C4, C5, P1)

Add a mandatory PLAN block at the top of `APPROACHES/index.md` (no new files), rewritten from scratch at every turn start and after every compaction, before any node file is opened:

```
PLAN (mandatory top section of APPROACHES/index.md, at most 30 lines). Rewrite it from
scratch, without copying the previous version, at the start of every turn and after
every context compaction, before opening any node file. Then diff against the old PLAN
and keep the better of each line.
- TARGET: the statement in one line.
- BEST ROUTE: at most 6 steps, each marked PROVED (lemma ID), STANDARD (black-box
  citation with its exact interface), or MISSING.
- MISSING STEP: a precise theorem statement.
- WEAKEST SUFFICIENT OBJECT: the weakest property of the missing object that the
  consuming lemma actually uses (quote the lemma and the proof line that uses it).
  If you are building something stronger, say why the weaker object is not enough.
- OBJECT OR PROCESS: state whether the missing object must be represented, or whether
  a single sample maintained by a repair step whose conditional expectation is bounded
  by induction would suffice. If you represent a law, say why a process cannot suffice.
- EXISTENCE LEDGER: every existence theorem already proved for that object, and for
  each, the auxiliary flow / LP / transportation problem whose feasibility it
  certifies, on which network, and what one computation of that problem yields.
  An existence theorem is a feasibility certificate until shown otherwise; never label
  it "non-algorithmic".
- FORMULATION LEDGER for the missing step (tried / untried; node ID and one-line
  failure reason if tried):
  (i)   existence theorem used only as a certificate;
  (ii)  one flow / circulation / LP on an auxiliary network plus expectation-preserving
        rounding;
  (iii) one maintained sample improved by an iterated random step whose one-step
        conditional expectation is a linear contraction toward the target (polylog
        steps; a full max-flow per step is affordable);
  (iv)  recursion / divide-and-conquer;
  (v)   polylog calls of a known almost-linear primitive;
  (vi)  a new dynamic data structure, optimizer, or sampler for a fixed law.
  Rows (i)-(iii) must be tried and closed in writing before any node under (vi).
- CALL BUDGET: how many calls of the most expensive primitive the outer loop makes.
  If not polylogarithmic, state why a polylogarithmic design is impossible.
- BRANCH BUDGET: author time spent on the current route (cap: strategy rule 5).
- AUDIT RESPONSES: one line per auditor recommendation: ADOPTED / REJECTED (reason or
  refuting lemma) / DEFERRED.
```

Add strategy rules before the node-format section:

```
STRATEGY RULES
1. Weakest object first. Design for the weakest property the consuming lemma uses
   (e.g. "a marginal bound on one random sample"), not for the object named in the
   literature.
2. Existence as certificate. When an object is known to exist (a packing, a polytope
   point, a feasible circulation), first design an algorithm whose proof cites only
   the existence while its computation touches a simpler object (one flow, one tree,
   one rounding, one average). For each existence theorem, write down the auxiliary
   network on which it certifies feasibility. Construct or maintain the object only
   after a written paragraph shows existence alone cannot suffice.
3. Object versus process. Before optimizing or representing a distribution, ask
   whether a single sample plus a repair step suffices. A marginal bound proved by
   induction over a random process needs no marginal computation.
4. Integralization checklist. Whenever a fractional object appears, record in the node,
   with a citation or a one-line rejection, each of: the polytope's inequality
   description and an averaged / lazy step; expectation-preserving (unbiased)
   rounding of flows and circulations (distinct from deterministic integral rounding);
   classical decomposition theorems (Edmonds' disjoint branchings, matroid partition /
   intersection, Hall / transportation feasibility). Look these up before proving
   obstructions.
5. Branch cap. A route (a top-level node and its descendants) gets at most 90 minutes
   of author time or 3 descendant nodes before it either changes the MISSING step in
   the PLAN or is set BLOCKED with an Obstacles section. A tool node must state in its
   header which MISSING step it closes; if it cannot, do not create it.
6. Obstruction cap. A negative result against your own sub-proposal goes in the
   parent's Obstacles section; it becomes a PROVED.md lemma only if it rules out an
   entire FORMULATION LEDGER row. Two consecutive obstructions on one route set the
   route BLOCKED and force a different ledger row.
7. Literature. Use the literature only for black-box primitives with exact statements
   and for the checklist in rule 4. Statements that the problem is open or hard are
   not evidence: do not record them as nodes and do not let them change the plan, the
   tone, or the decision to deliver a progress report. Importing machinery found by
   search requires a PLAN line "simpler primitive X fails because Y".
8. Progress is a PLAN step moving from MISSING to PROVED, or a ledger row moving from
   untried to tried with a reason. Node and lemma counts are not progress; never
   report them.
```

Node header and status semantics (replace the four-line header):

```
# A### — Title
Parents: ...   Children: ...   Status: ACTIVE | BLOCKED | CLOSED | RESOLVED.
Closes the MISSING step: yes / partially (which step) / no.
RESOLVED means the node's local objective is achieved. A RESOLVED node marked "no" is
a tool and counts toward the branch cap. Openings define only terms absent from the
statement and PROVED.md; link lemma IDs instead of restating them (at most 12 lines).
```

Turn-end contract: "End every turn by pasting the current PLAN verbatim and nothing else about the records. Do not write a progress report unless the controller asks for one."

Why this targets the documented failure: at 15:33 the PLAN would have had to say "MISSING: sample an arborescence with marginals ≤ (3/2)c/λ; OBJECT OR PROCESS: undecided; EXISTENCE LEDGER: L019 certifies feasibility of ? on which network?; ledger rows (i)–(iii) untried". Rows (i)–(iii) are I3–I5; the rule-4 checklist is I0/I6/I7/I8. Rule 5 would have blocked A019 around 17:20 instead of letting it spawn A035–A045; rule 6 would have stopped the L059–L062 and L065–L070 loops after two lemmas; rule 7 removes the progress-report mode.

### 6.2 Continuation, compaction and goal prompts (targets P2, P5, C1)

The YAML `continuation` is not delivered (see 6.4), so its content must go into `goal` or be sent by the runner at every turn boundary.

```
goal: >-
  Resolve the exact statement in INITIAL_PROMPT.md with a complete, rigorous solution.
  At the start of every turn rewrite the PLAN section of APPROACHES/index.md from
  scratch and work on the cheapest untried ledger formulation of its MISSING step;
  maintain the other records as INITIAL_PROMPT.md defines.

continuation: >-
  Continue the same statement. Before touching any node file: (1) reread the
  STATEMENT and the PLAN; (2) rewrite the PLAN from scratch in your own words, then
  diff it against the old one and keep the better of each line; (3) name the single
  MISSING step and the ledger rows already tried with their failure reasons; (4) pick
  the cheapest untried row and spend this turn on it. Do not re-verify the index,
  links, or old lemmas unless a lemma you are about to use is in doubt. End the turn
  by pasting the PLAN. Do not report counts of nodes or lemmas.

compaction: >-
  Context was compacted. Read INITIAL_PROMPT.md, then only the PLAN section of
  APPROACHES/index.md and the one node you were writing. Do not check the index,
  links, or file structure now. Restate in two lines the MISSING step and what the
  current node contributes to it; if you cannot state the contribution, mark the node
  BLOCKED and return to the PLAN.

resume (append after "Continue the same goal."): >-
  Rewrite the PLAN first. Answer each auditor status verdict and numbered direction in
  the PLAN's AUDIT RESPONSES. A STOP verdict from two or more auditors on one node sets
  it BLOCKED until you record a rebuttal in that node. Do not re-verify the index.
```

### 6.3 Audit prompt and schedule (targets P4, C1, C2)

Replace the `prompt:` of `workflows/research_audit.yaml` with a phased structure that forces an independent plan and a premise audit before the records are read, and demotes readability:

```
PHASE 0 (before opening APPROACHES/ or PROVED.md): read INITIAL_PROMPT.md only and
write a one-page plan of how you would attack the statement today, naming the two or
three standard theorems you would lean on and, for each, whether you would use it
constructively or only existentially. Save it verbatim as Section 1 of your report.
PHASE 1: read APPROACHES/index.md (PLAN first), every ACTIVE node, then the lemmas they
cite. Report which files you read in full.
PHASE 2 (premise audit), with lemma IDs: (a) for each ACTIVE node, the WEAKEST object
its downstream consumer needs versus the STRONGER object the node builds; (b) every
existence lemma the author is trying to make constructive, and whether existence plus
a feasibility / LP / flow argument on some auxiliary network would suffice; (c) whether
the author represents a law that a maintained sample plus a repair step could replace;
(d) the author's top three unexamined assumptions and how your Phase-0 plan differs on
each; (e) which of your Phase-0 ingredients has no node.
PHASE 3 (verdicts): for every ACTIVE node exactly one of CONTINUE (name the single
theorem that would finish it), STOP (evidence: k consecutive descendants ending in
"bound remains open", or a lower bound the author already proved), REDIRECT (name the
replacement objective). Never CONTINUE a node whose last three descendants added only
interfaces, static tools, or counterexamples to the author's own proposals, unless you
name the missing amortization argument.
PHASE 4: at most three new directions, ordered by expected value, each naming the
Phase-2 gap it exploits and the first lemma to prove. Label every mathematical claim
VERIFIED / CHECKED-SMALL (ran a computation on small cases) / CONJECTURE. Never present
a conjecture as a theorem.
PHASE 5 (at most 15% of the report): stale statuses, index rows contradicting node
files, broken links, undefined first uses in ACTIVE nodes. No structural proposals.
FORMAT: 10-line executive summary first (verdict per ACTIVE node, top premise gap, the
one direction to pursue first); whole report under 2,500 words.
```

Schedule and mechanics: run auditors on a read-only snapshot every 60 minutes of author time without pausing the author (they are already read-only); time-box each auditor at 20 minutes; have the controller merge the three reports into a one-page verdict tally (per ACTIVE node STOP/CONTINUE counts, union of directions) and hand the author that page, not 55 KB of reports, at its next turn boundary; convert one auditor slot into the fresh-eyes solver of 6.5.

### 6.4 Runtime changes in `workflow_runner.py` and the YAML (targets P1, P2, P5, P6)

1. **Deliver the continuation.** On every `turn/completed` while the goal is active (not only `status in {"blocked","complete"}`, lines ~1348–1349), send `render("continuation")` via steer or `start_turn`, or fold its text into `goal`. Zero deliveries in this run.
2. **Checkpoint before compaction; lower the ceiling.** In the `thread/tokenUsage/updated` handler, when `last.inputTokens` crosses ~140k, steer "rewrite the PLAN now, then continue"; drop `model_auto_compact_token_limit_scope="body_after_prefix"` (lines ~216–222) or set an explicit lower limit. Compactions happened at 167–197k and the fatal one at ~229k.
3. **Freeze INITIAL_PROMPT.md during a run.** Hash it at segment start; on change, pause with a status line instead of letting the author migrate 32 nodes mid-turn; apply format changes at a segment boundary.
4. **Re-anchor subagents or make them short-lived.** Send the compaction steer to subagent threads too (`if not is_root(message): continue`, lines ~1280–1281); prefer one-task spawns for verification and writing chores.
5. **Quota alarms and pacing.** Sum token usage across all threads per hour, emit a status line, throttle above a configured rate; count auditor models on the same provider against the same budget.
6. **Turn hygiene and flush.** Force a turn end at least every ~30 minutes (the last turn ran 113 minutes); on any error without `willRetry`, write pending subagent results and the root's pending message to `partial-output.md` before pausing.
7. **Enforce audit verdicts.** Parse per-node STOP/CONTINUE; two STOPs flip a node to BLOCKED in `index.md` and the node file unless the author's next PLAN carries a rebuttal line.
8. **Record-size validator.** Reject a node write above ~25 KB without a ≤ 40-word abstract at the top and a "Detailed work" marker below which compaction recovery does not read.
9. **Complete file-event capture.** `filechanges` recorded 3 of 73 PROVED.md writes and stopped at 19:48; a post-mortem needs every write with a timestamp.

### 6.5 Fresh-eyes solver subagent (targets C1–C3, P3, P4)

- **Trigger:** every 60 minutes of author time and at every audit batch; also whenever the PLAN's MISSING step has not changed for two consecutive turns.
- **Isolation:** a fresh thread, preferably a different model from the author; read access only to INITIAL_PROMPT.md and a controller-generated one-page brief containing the PLAN and the statements (not proofs) of the PROVED lemmas on the BEST ROUTE. No access to APPROACHES/, the rest of PROVED.md, AUDITS/, or web search for the first 20 minutes.
- **Prompt:**

```
You are a solver, not a reviewer. Read the STATEMENT and the one-page BRIEF only. Do
not assume the brief's framing is right. Answer in order, in at most 1,500 words:
1. What is the WEAKEST object the brief's proved lemmas actually consume (quote the
   lemma)? Is the brief's MISSING step stronger than that? If so, restate the missing
   step for the weakest object.
2. For every existence theorem in the brief, name the auxiliary flow / LP /
   transportation problem whose feasibility it certifies, on which network, and what
   one computation of that problem yields.
3. Give a complete candidate algorithm for the missing step using only these
   primitives unless you show they cannot suffice: one exact max-flow or min-cost
   flow per step, expectation-preserving rounding, an averaged / lazy step inside a
   polytope, a classical decomposition theorem, a single maintained sample updated
   polylogarithmically many times. State its per-step guarantee as an inequality on
   conditional expectations, and its number of steps.
4. List every gap in your candidate honestly, ranked; mark each VERIFIED /
   CHECKED-SMALL / CONJECTURE.
Do not write records. Return the text to the controller.
```

- **Root obligation:** the controller appends the solver's text to AUDITS/ as `fresh-eyes-<time>.md`; the author's next PLAN must carry one line per solver gap (ADOPTED with node ID / REJECTED with reason). If the solver names a ledger row the author marked untried, the next node the author opens must be on that row.
- **Cost:** one thread for 20–30 minutes per cycle, roughly 5–10% of tokens.

### 6.6 Record diet (targets P1, P2)

Keep INITIAL_PROMPT.md, APPROACHES/, PROVED.md, AUDITS/. Change the writing rule from "every node self-contained" to "define only terms absent from the statement and PROVED.md; link lemma IDs instead of restating them; opening at most 12 lines; abstract at the top". Move proofs of obstructions against the author's own proposals into the parent's Obstacles section (rule 6). This alone would have removed most of the 2.3 MB the author wrote and re-read.

---

## 7. A cheap pilot before the next long run

The improvements above are hypotheses. Two experiments cost less than an hour each and would tell you whether they bite on this problem:

1. **Fresh-eyes pilot.** Give the 6.5 prompt to one or more models with only the statement and the statements of L001, L006, L007, L019 as the brief. Success criterion: does any model's answer to question 3 propose a single-tree contracting step with a flow-feasibility certificate, or at least reach ledger row (iii)? If none does within two attempts, the prompt needs the integralisation checklist inline, and the recommendation should be revised.
2. **Replay with the new author prompt.** Re-run the same statement with 6.1–6.2 in place and a 2-hour cap. Success criterion: the PLAN's FORMULATION LEDGER shows rows (i)–(iii) tried with reasons before any node under (vi) is opened, and no route exceeds the branch cap. Whether it finds I4–I8 is the real test, but the ledger discipline is observable regardless.

Do not resume the paused run on Sep 20 in the current regime: its direction at cutoff was a Markov-chain sampler for an optimised product law, the right process shape attached to the wrong commitment.

---

## 8. Open questions the evidence cannot settle

1. What the root actually reasoned at 15:47, 15:51, 20:58, 21:13, 21:26: Codex stores only reasoning-summary titles, so whether the polytope and rounding ideas were dismissed for a reason or merely dropped is unknowable.
2. Root-to-subagent task texts are not in the transcript; the pivots at 15:51, 19:18 and 20:55 are inferred from subagent echo messages.
3. Whether gpt-6-astra, given the 6.1 rules or the 6.5 prompt, would produce the half-step lemma: the run contains no such experiment, and the six audits (fresh threads that saw the statement) failed the same way.
4. Whether L034's block gadget is a drop-in for PDF Lemma 5.1 (it needs capacities and a path-decomposition argument); nobody proved it.
5. Who edited INITIAL_PROMPT.md at ~15:27 and ~16:02; the git commit at 16:20 UTC dates the YAML change, not the run-directory file.

---

## Implementation

The strengthening changes of section 6 are implemented on the git branch `fable-suggestion` (author prompt with PLAN block and strategy rules, lifecycle prompts, phased audit prompt with a digest and a fresh-eyes solver, continuation delivery, pre-compaction checkpoint, subagent re-anchoring, unsaved-result flush). The separate cost-efficiency proposal and its implementation status are in `EFFICIENCY.md`.

## Files in this folder

- `REPORT.md` (this file).
- `EFFICIENCY.md`: the separate cost-efficiency proposal (xean comparison, measured cost drivers, ranked changes with branch status, measurement plan); its evidence is under `specialist-reports/efficiency/`.
- `appendix-A-verified-synthesis.md`: the workflow's synthesis after adversarial verification, with its verification log and the 20 load-bearing claims.
- `appendix-B-verifier-objections.md`: the three verifiers' refutations and general objections.
- `specialist-reports/`: the shared brief, the flattened transcript narrative, and the 19 specialist analyses (PDF soundness; time windows W1–W7; approach clusters; audits; prompts; runtime; two counterfactuals).
