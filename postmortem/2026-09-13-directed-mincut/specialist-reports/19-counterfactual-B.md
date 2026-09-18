## Report

### 0. Scope and method

Verified sources: brief.md, pdf.txt (all 563 lines), A006, A010, A028, A051, PROVED.md L006/L007/L012/L013/L019/L023/L024/L025/L034/L063/L067/L068, INITIAL_PROMPT.md, index.md, both audit batches, author_critic.yaml, research_audit.yaml, partial-output.md, pause.json, and targeted greps over narrative.txt (10009 lines), subagents.txt, websearches.txt, filechanges.txt. Narrative THINK lines are one-line summaries of hidden reasoning; I treat them as evidence that a topic was touched, not of its content. Everything labelled "inference" is my reconstruction.

### 1. The author's actual end state against the PDF's ingredients (verified)

| PDF ingredient | Present? | Where |
|---|---|---|
| I1 centroid recovery | Yes, identical | L006 (PROVED.md:187–210), written 15:13–15:14 |
| I2 marginal criterion E|T∩δ(S*)| < 3/2 | Yes, generalized to undirected trees + mean tail degrees | L007 (:211–232); L063 (:6197+), A051 lines 31, 41–99 |
| I3 Edmonds existence x ≤ c/λ | Yes, as LP duality, tagged non-algorithmic | L019 (:439–474): "No running-time or small-support representation bound follows" |
| I4 contraction of one tree's marginals | No | 0 hits for any conditional-expectation halving; see §4(i) |
| I5 exchange network as bounded circulation | Half: the graph exists, the labels differ | A028 lines 12–14, 32: vertex per arc; e→f for tree arcs f on the fundamental path; f_u→e same-tail. Arc-for-arc this is PDF Def. 2.1 classes (3)–(4) mirrored. A028 labels arcs with weights ℓ_e−ℓ_f and asks for negative cycles; the PDF labels nodes with throughput bounds [1−q_e,1], [0,q_f] and asks for a feasible circulation. The lower-bound-circulation technique is in L013 (:344–356). |
| I6 half-step lemma | No, but its only nontrivial inequality is in the record | PROVED.md:6306–6308 (L063 §2): "the induced graph of a spanning tree is a forest, so x(E(S)) ≤ |S|−1" |
| I7 unbiased rounding | No | narrative: unbiased 0, pipage 0, swap rounding 0, dependent rounding 0, Kang/Payor 0; all 86 "rounding" hits are numeric |
| I8 two-arborescence decomposition | No | Tarjan 0, disjoint branchings 0, arc-disjoint 0 in narrative; Edmonds 2 hits (15:25:38, 15:26:19), both min-cost-branching dual for L017–L019 |
| I9 compression | Yes, smaller (O(m+n log n)) | L034 (:1118–1144), A028 lines 36–58; heavy-light at 19:14:08 (narrative 5925) |
| I10 geometric guesses, dyadic targets | Yes in analog form | L023 sets K from a 9/8-approximate ρ̂ (narrative 1553–1558); dyadic discipline L071 |
| I11 almost-linear exact flow | Yes | A001; A006 line 74 |

Conclusion (verified): by 21:02 UTC the author held I1–I3, I9–I11, the exchange graph of I5, the proof technique of I5 (L013), and the key inequality of I6. Never appeared: the process view (I4), the bounds-not-weights relabelling of A028 (I5), the integrality step (I7–I8).

### 2. Minimal chain of insights from the actual state to the PDF (inference, tied to what was in hand)

C1. Only one tree's marginals matter, so maintain one tree and track p_t = E[1_{T_t}]. In hand: L024 (narrative 1544, 15:37:34: "Explicit packing support is unnecessary … independent samples from the final packing") + L063 (A051:31 "individual samples need not be in-arborescences"). Missing: combining them into "the distribution never needs to be represented".

C2. Demand E[1_{T'}|T] ≤ (1_T+q)/2 with q ≈ c/L; then t = O(log n) steps, so each step may be a full max-flow. The one genuinely new idea; it turns the O(m log N)-event MWU (L023) into O(log n) events.

C3. Encode one step as a feasible circulation with bounds on the A028 exchange graph; feasibility from L019's x ≤ q via the L013-style cut/Hall argument on fundamental paths. In hand: A028 graph, L013 technique, L019 existence. Missing: "what does a flow compute on this graph?" (H5).

C4. Show (1_T+F)/2 is in the arborescence polytope via x(E(S)) ≤ |S|−1, splitting on whether T[S] is connected. In hand: the inequality (L063 §2). The case split answers A028's own repeated worry that a found cycle need not give "a valid simultaneous exchange" (A028 lines 26, 34, 58, 70) — the PDF's Remark 3.2 says the same and sidesteps it with the 1/2 factor.

C5. Round the circulation unbiasedly (dyadic Euler-tour or Kang–Payor), split T+B into two arborescences (Edmonds/Tarjan), pick one. Not in hand; textbook; never searched (websearches.txt: 0 of 209 searches contain rounding/Edmonds/Gabow/branching).

C6. Wrap-up: L034-style compression works for flow (inference: A028's "first original-arc vertex reached" argument is PDF Lemma 5.1's path decomposition), L023's ρ̂ gives the geometric guess, L071's grid gives bit complexity, A006 lines 78–88 give amplification.

Three new insights (C2, C4, C5); C2 is the bottleneck. Most plausible trigger (inference): at 20:58:13, right after L063 said "marginals suffice", the author's next action was the web search "maximum entropy spanning tree distribution prescribed marginals algorithm" (narrative 7845) — a search for a distribution. A forced written answer to "Do I need a distribution, or only a process whose single output has the right expectation, and how many calls to my most expensive primitive can that process afford?" at that moment would have produced C1–C2 with L013/L019/A028/L063 already on file, so C3–C4 within reach the same hour. The earlier candidate trigger is 15:49:16 (A019 created, filechanges.txt), when the author committed to O(m log N) oracle events; that spawned the 14-node A019 cluster (A019, A021, A025, A028, A035, A037–A045) from 15:49 to 19:48:53, roughly four of six author hours.

### 3. Five heuristics that would most likely have surfaced the PDF idea

H1 — Object vs. process. "Which do I need to represent: the whole distribution, its marginals, or one sample? What is the cheapest random process whose single output has the required expectation?"
- Moment: 20:58:13 (narrative 7845) and 21:26:30 (narrative 8752: "constructing a suitable distribution in almost-linear time remains unproved"). Both times the author had just proved a marginals-only sufficient condition (L063, L067) and reached for a global optimization (A052–A060, 10 nodes in the last 65 minutes).
- Why not asked: A006 line 71 fixed the premise as "a sampler for a feasible packing"; index.md:68 froze it as "the master reduction"; the frame comes from the Karger/CLNPS/JLSW literature read in the first 2 minutes (15:07:50). Literature anchoring plus RESOLVED nodes treated as fixed background.

H2 — Existence as a feasibility certificate. "For each existence theorem I labelled 'not an algorithm', which LP or flow does it certify feasible?"
- Moment: 15:22–15:26. A010 line 43 on L013: "not an algorithm to find S"; L019: "No running-time … bound follows". The PDF uses Edmonds' existence exactly once, as Corollary 2.3, to certify the exchange circulation is feasible for every T.
- Why not asked: verification discipline in INITIAL_PROMPT.md (A006:102 "not substituted for an implementation") files existence results as closed; both batch-1 audits reinforced it (Claude ...1ad9413a.md:33: mark A010/A012/A015 CLOSED; Kimi ...6e3c6013.md:86: "conclusively closed by L025 … do not reopen").

H3 — Call budget before data structures. "Write the outer loop with the missing piece as a black box. How many calls? If polylog, each call may be a full max-flow; if ~m, I am choosing a dynamic-data-structure problem — is that forced?"
- Moment: 15:37:34 → 15:49:16. L023 delivered O(m log N) events and the next node A019 defines a "dynamic tree oracle". The PDF's design is that O(log(mU) log² n) full max-flows are affordable.
- Why not asked: MWU anchoring; sunk cost — batch-1 Claude "Direction B: reframe A019 as subtree re-hanging" pushed deeper; "cap this line" arrived only at 20:13 (Kimi batch 2 §3.1).

H4 — Fractional then round. "If the fractional version of my object is one flow away, what unbiased rounding makes it integral? E[round(x)] = x is all a marginal argument needs."
- Moment: 21:21:50–21:26:30. L067: "one maximum-flow instance … supplies either a certificate or a violating side"; ROOT THINK 21:26:10 "Analyzing advanced matroid and flow rounding techniques" (narrative 8729) left no trace in any file; 20 seconds later the message returns to "constructing a suitable distribution".
- Why not asked: "rounding" meant numerical precision in this record (all 86 hits); the known sampling stack was determinantal (Schild, Anari, Laplacian solvers — websearches.txt 22–40, 137–204), where one samples a law rather than rounds a point.

H5 — Re-label every auxiliary structure with every primitive you own. "For each auxiliary graph I built, what does each almost-linear primitive (max-flow with lower bounds, min-cost flow, negative cycles) compute on it?"
- Moment: 16:20–16:35. A028 built and verified only for weighted walks (16:35:01, narrative 3548). L034 never reused afterwards (3 narrative mentions after line 3600; only A019 and A051 cite it).
- Why not asked: A028 inherited A019's cost objective; the mandated "Objective" section asks what a node targets, never what else the object could do; batch-1 Claude (:47) declared L034 "low value … would require negative-cycle detection".

### 4. Explicit answers

(i) Markov-chain / iterative single-tree improvement toward target marginals? No such thing; three near misses with different objectives. (a) 15:17:04 SUB:structural_approaches THINK "Exploring Markov chains for arborescence sampling" — summary only; subagents.txt has zero Markov/chain content. (b) L023/L024 (15:37): one "current tree" evolves through O(m log N) oracle-driven replacements with reservoir snapshots — driven by a load potential and a min-cost oracle, not marginals. (c) 22:03:30–22:05:43: batch_supersets verified the Anari et al. down-up walk ("applies from any starting tree … adds a uniformly chosen non-tree edge, then removes a cycle edge with probability proportional to its reciprocal activity", narrative 9965) — target is a stationary product law; it surfaced 54 seconds before the usage-limit error at 22:06:37. Batch-1 Claude "Q2 greedy single re-hang" (...1ad9413a.md:72) is a cost step, never pursued (re-hang hits only 4205, 4305). No line uses E[1_{T'}|T] ≤ (1_T+q)/2 or any marginal contraction.

(ii) Polytope inequality / Edmonds disjoint branchings constructively? The inequality x(E(S)) ≤ |S|−1 is used once, existentially: PROVED.md:6306–6316 (L063 §2) derives "the rooted minimum under capacities x is exactly one" then invokes L019; A051:127 "without importing an algorithmic matroid-intersection theorem"; A051:131 "The conversion through L019 is only an existence proof." Edmonds' disjoint-branchings theorem is never named; the four arc-disjoint hits in PROVED.md (800, 1293, 1330, 1382) are hand-built tree pairs certifying ρ = 2 in counterexample families. Kimi batch-1 (:95) cited "Edmonds–Gabow fractional arborescence packing theorem" for L019 — the closest anyone came. Verdict: inequality yes-but-existential; disjoint branchings no; nothing constructive.

(iii) "We don't need the packing, only its marginals / only one sample"? Yes, twice by the author's team, once by an auditor, never with the process consequence. (a) 15:37:34 (narrative 1544): "Explicit packing support is unnecessary. Maintain h independent weighted reservoirs …" → L024. (b) 20:58:22 ROOT (narrative 7851): "undirected tree samples can suffice if their average outgoing degrees meet exact constraints. That could avoid maintaining directed trees"; A051:31, A051:256 "An explicit decomposition into directed arborescences is unnecessary"; narrative 8790 (21:23): "without first recovering highly accurate individual marginals." (c) Kimi batch 2 (...d0251787.md:31): "(P) … or replace it by a packing-free exact search" — meaning the few-crossing search. Every time the next action was to look for a distribution (20:58:13 max-entropy search; A056–A060), never a process on one tree.

### 5. Ranked instruction changes

1. Mandatory turn-start design card. Before any new node, ≤10 written lines answering H1 (represent vs. expect), H3 (call budget; cost allowed per call), H5 (for each auxiliary structure, which owned primitives were tried). Justification: the two pivotal decisions (15:49 O(m log N) events; 20:58 max-entropy search) were made inside THINK summaries with no written rationale, while INITIAL_PROMPT.md:32 demands "state clearly what you are targeting" but never "why this target rather than a cheaper one."

2. Ration obstruction lemmas and record-keeping. Each obstruction node names the goal-level claim it kills and counts against a budget; index/link maintenance is delegated to a script or subagent. Justification: 19 of 73 lemmas are lower bounds against the author's own sub-proposals; 31 of 88 root messages mention indexing; a human steer at 16:39 was needed for the index; 20 of 125 subagent results are pure verification passes; research_audit.yaml goal 2 spends half the audit prompt on readability, so audits returned readability advice instead of the missing framing.

3. Existence-to-certificate and primitive-relabelling pass before any BLOCKED/CLOSED status. For each existence lemma, list the LP/flow whose feasibility it certifies; for each auxiliary graph, what max-flow-with-bounds / min-cost-flow / shortest-path would compute on it. Justification: L013 (15:22) and L019 (15:25) were filed as "not an algorithm"; A028's exchange graph (16:20) is arc-for-arc PDF Definition 2.1 with weights instead of bounds; both batch-1 audits then told the author to keep those branches closed.

Secondary (verified): the run ended on a Codex usage-limit error at 22:06:37 (pause.json), 71 minutes after the A051 pivot that put every ingredient except C2/C5 into the record; instruction changes should be judged against a run that reaches its time budget.

---

## Key findings

- [verified] By 21:02 UTC the author held every PDF ingredient except the single-tree marginal contraction (I4), the bounds-not-weights reading of the exchange graph (I5), unbiased rounding (I7) and the two-arborescence decomposition (I8).
  - evidence: L006/L007/L019 (PROVED.md:187-232, 439-474); L063 sec.2 at PROVED.md:6306-6316 uses x(E(S)) <= |S|-1; L013 (PROVED.md:344-356) is a lower-bound circulation feasibility proof; A028 lines 12-14, 32 define the exchange graph arc-for-arc as PDF Def 2.1 with weights; L034 compression; narrative counts: unbiased 0, pipage 0, Kang/Payor 0, Tarjan 0, disjoint branchings 0, arc-disjoint 0.
- [verified] A028's exchange graph is structurally identical (up to in/out mirror) to the PDF's exchange network; only the labels differ (weights and negative cycles vs throughput bounds and feasible circulation).
  - evidence: A028 line 14/32: e->f for tree arcs f on the fundamental path, f_u->e for same-tail; PDF Def 2.1 classes (3)-(4): e_v^out->f^in same-head, f^out->e^in for e on P_T(f).
- [verified] The author never considered a Markov-chain or iterative single-tree process aimed at target marginals; the three single-tree processes that appeared targeted a load potential (L023/L024), a cost improvement (audit Q2 re-hang), or a stationary product law (Anari down-up walk at 22:05:43, 54 s before the usage-limit cutoff).
  - evidence: narrative 519 (15:17:04 THINK only, no subagent content), 1544 (L024 reservoir), audit_history claude report line 72, narrative 9965 (22:05:43), error at 22:06:37; no hit for any conditional-expectation halving.
- [verified] The arborescence-polytope inequality was used exactly once and only existentially (L063 sec.2 via L019); Edmonds' disjoint-branchings theorem and Tarjan's algorithm were never mentioned.
  - evidence: PROVED.md:6306-6316; A051:127 and A051:131; narrative Edmonds hits only at 15:25:38 and 15:26:19 (min-cost branching dual); PROVED.md arc-disjoint hits (800,1293,1330,1382) are hand-built counterexample trees.
- [verified] "We don't need the packing, only one sample / only its marginals" was stated twice by the author's team (15:37:34 L024 reservoir; 20:58:22 A051/L063) and once by an auditor (Kimi batch 2, packing-free search), but each time the follow-up action was to search for a distribution rather than a process.
  - evidence: narrative 1544, 7851, 7845 (web search 'maximum entropy spanning tree distribution prescribed marginals' at 20:58:13), 8752, 8790; A051:31, A051:256; AUDITS kimi report line 31.
- [likely] The minimal chain from the author's state to the PDF is three new insights (half-step contraction C2, half-step lemma C4, unbiased rounding + two-arborescence decomposition C5); C2 is the bottleneck and the most plausible trigger was a forced 'distribution vs process / call budget' question at 20:58:13, or earlier at 15:49:16 when A019 committed to O(m log N) oracle events.
  - evidence: Reconstruction from the ingredient table; A019 created 15:49:16 (filechanges.txt:9 region) spawning 14 nodes until A045 at 19:48:53; L063 completed 21:02:36; web search at 20:58:13.
- [verified] The author's notion of 'rounding' was exclusively numerical precision; the only possible brush with randomized flow rounding is a 20-second THINK at 21:26:10 that left no trace in any file.
  - evidence: All 86 narrative 'rounding' hits concern dyadic grids, dual labels, cost rounding; narrative 8729 THINK 'flow rounding techniques'; no 'flow rounding' in APPROACHES or PROVED.md; next message 21:26:30 returns to 'constructing a suitable distribution'.
- [verified] Instruction design and audits reinforced closing existence results and the circulation branch, removing the objects the PDF reuses.
  - evidence: A010:43 and L019 statement label existence as non-algorithmic; audit_history claude report line 33 (mark A010/A012/A015 CLOSED) and kimi report line 86 ('do not reopen'); claude line 47 calls L034 low value; A006:102.
- [verified] Record-keeping and verification consumed a large share of author bandwidth: 31 of 88 root messages mention indexing, 20 of 125 subagent results are pure verification passes, 19 of 73 lemmas are obstructions against the author's own sub-proposals, and one human steer (16:39) was needed for index upkeep.
  - evidence: grep counts over narrative.txt and subagents.txt; brief.md lemma list; INITIAL_PROMPT.md self-containment rules; research_audit.yaml goal 2 devotes half the audit prompt to readability.
- [verified] The run was cut off by a Codex usage-limit error at 22:06:37, 71 minutes after the A051 pivot, not by the research budget.
  - evidence: pause.json error field; narrative 22:06:37-22:06:42 ERROR lines; A051 completed 21:02:36.
