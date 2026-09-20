# Directed minimum-cut trajectory review

Review date: 2026-09-20. The principal lesson is **to search for a cheaper sufficient invariant before building machinery for a stronger oracle**. The run already had the cut-recovery reduction and a useful tree-exchange representation. The supplied draft connects these with a fractional repair, rounding, and a two-tree decomposition. The run instead spent much of its effort on dynamic packing, historical decompositions, marginal estimation, and numerical optimization. Several of those targets carried multiple independent unresolved complexity bounds.

This is a diagnosis of the saved research and a proposal for improving the agent. It is not a claim that a workflow change guarantees discovery of a new theorem. The supplied PDF calls itself a candidate proof awaiting external review. I found no defect in its central argument in this review, but do not certify the entire research literature or claim an independently discovered solution.

## Evidence and coverage

The [machine-readable inventory](mincut-evidence-inventory.json) records the exact source paths, file hashes, all approach files, all `PROVED.md` lemma headings, all audit reports, transcript event counts, and timestamp ranges. The source run folders and PDF remain unchanged.

For readability, the following aliases refer to exact directories under `runs/`:

| Alias | Directory | Transcript lines | Transcript bytes | Approach nodes, excluding index | Lemma headings |
| --- | --- | ---: | ---: | ---: | ---: |
| R0 | `2026-09-11_20-36-05_design-and-analyze-an-algorithm-for-exact-global` | 459,240 | 1,281,650,101 | 4 | L001-L204 |
| R1 | `2026-09-12_17-24-47_directed-mincut-migrated` | 99,800 | 143,816,265 | 74 | L001-L329 |
| R2 | `2026-09-13_15-00-00_directed-mincut-prepared` | 8,652 | 5,520,261 | 12 | 17 retained/new headings, through L340 |
| R3 | `2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global` | 81,550 | 68,484,347 | 60 | L001-L073 |

Every one of the 649,242 JSONL lines in these four directories was parsed. Completed items were deduplicated by `(threadId, item.id)` within each run. Every approach, index, lemma heading and audit was indexed and searched for the relevant hypotheses, conclusions, barriers and repair mechanisms. There are 150 approach-file instances, 216 Markdown files, and 193 byte-distinct Markdown files totaling 13,564,734 bytes. The 42 audit-file instances represent 24 byte-distinct reports; R2 includes copies of R1's 18 reports. These counts are a file census, not a count of independent discoveries.

Detailed reading concentrated on the master reductions, the closest exchange/flow constructions, the live frontiers, and audit recommendations: R0 A003 and its L013/L014 recovery contract; the R0/R1/R2/R3 navigation and obligation maps; R2 A074/A080; R3 A006, A010, A028, A051, A055 and their relevant proof statements; and the audit passages cited below. All eleven PDF pages were read in extracted text, with the central half-step and rounding pages also inspected as rendered images. A finite exhaustive checker for its central integral invariant was rerun.

**Limit:** this is trajectory-wide indexing plus targeted mathematical review, not a fresh line-by-line verification of every proof in more than 13 MB of unique records, or access to hidden model reasoning. Intermediate file revisions embedded in command outputs and patches were not all reconstructed into separate snapshots and semantically audited. The saved messages and research artifacts support the causal diagnosis; they cannot prove what the model would have discovered under different prompts. Token counters can reset across resumptions; neither final cumulative snapshots nor transcript byte size should be treated as total billed usage. Cost analysis is separate; use [tools/analyze_run.py](../tools/analyze_run.py) for observed usage with counter-reset accounting.

The earlier September 11 reachability/SCC runs solve a different problem. They were inventoried to establish scope and excluded from the min-cut diagnosis. `2026-09-11_20-18-54_solve-directed-minimum-cut-in-almost-linear-time` contains the initial statement review, not a substantive research trajectory.

## What happened

Timestamps below are UTC; directory names use the machine's local time.

| Stage | Saved trajectory | What advanced | What remained |
| --- | --- | --- | --- |
| Sep 11 18:36 to Sep 12 14:07, R0 | Four broad nodes; A001 grew to 1,447,064 characters | Rooted reductions; supplied-tree recovery L013; crossing bound L014; balanced-case construction L032; progressively richer historical/exchange phases L136-L204 | General reduction to those structured inputs; total phase/history size; exact output extraction; numerical optimization |
| Sep 12 15:24 to Sep 13 07:20, R1 | Migration split the archive into A001-A074 | Explicit phase LP/flow compilation, two-entry recovery, all-price boosting, coarse genuine-tree sampler L290, stochastic upper-load boost L311, backbone/forest alternatives L327-L328, two-interval structure L329 | A universal price-compatible construction and near-linear total access/repair work |
| Sep 13 14:39-14:59, R2 | Curated continuation retains 12 nodes and 17 lemmas | L337-L340 sharpen shared quotient, completion and localization obstacles and local construction | The same general construction/progress gaps |
| Sep 13 15:06-17:11, R3 | Fresh numbering; root/flow/literature/structural agents | L006/L007 recover the main reduction; weighted event bound L023; sampling accounting L024; static exchange compression L034 | Tree selection/maintenance, adaptive marginal access, or a different witness generator |
| Sep 13 17:11-20:13, R3 | Two audit batches; local repair and direct-split routes | A035-A045/L043-L056 add frozen certificates, local repairs, rounding and scoped obstructions | Whole-sequence work bounds; useful split selection |
| Sep 13 20:13-22:06, R3 | A046-A060 | Batched superset queries L057; ordering obstructions; undirected exposure L063; entropy duals; constant objective-gap sufficiency L068; finite witnesses L071 and local descent L072/L073 | Constructing and sampling the required distribution in almost-linear total time |

R3 ended at `2026-09-13T22:06:42Z` with `usageLimitExceeded` during remote compaction. Its `pause.json` and final transcript error agree. It was an unfinished investigation interrupted by quota exhaustion, not a completed proof followed by a failed critic.

R3 has 1,092 distinct completed command items, 209 web searches, 338 assistant messages, 3,580 reasoning-summary items and 30 compactions. These are orchestration measurements, not independent mathematical steps or token counts. `job-settings.json` records Astra with ultra effort and a one-hour audit interval for the latest run; actual batches pause research and take time, so the user's recollection of roughly two-hour audits should not replace the recorded schedule.

The archive is scientifically more careful than a simple “failed to reason” description suggests. It repeatedly distinguishes conditional reductions from solutions, records counterexamples, and corrects bad claims. R1 `PROVED.md` explicitly withdraws L077. R3 A006 corrects an auditor's claim about the two-crossing packing threshold, and A034 corrects an overbroad lattice claim. These practices should be preserved.

## The mathematical route the draft supplies

The PDF uses out-arborescences and incoming rooted cuts; R3 mainly uses in-arborescences and outgoing cuts. Reverse all arcs before comparing formulas.

Let `T` be the current rooted out-arborescence, and let `q` dominate the marginals of some feasible arborescence distribution. That distribution is an **existence witness**, not an object the algorithm constructs.

1. **Build an exchange circulation around this one tree.** A selected tree arc has replacement throughput in `[1-q_e,1]`; an unselected arc has throughput in `[0,q_f]`. Same-head arcs enforce one incoming unit at each nonroot vertex. Fundamental-tree-path arcs record possible graphic exchanges. Define `F_e=1-z_e` on tree arcs and `F_f=z_f` on other arcs. The result has `0<=F<=q` and exact incoming degrees.
2. **Prove feasibility from an unknown feasible marginal vector.** The PDF's Lemma 2.2 uses a capacitated Hall argument. For any set of non-tree arcs, the union of their tree paths forms a forest; graphic rank inequalities bound their total demand by available removal mass. No packing enumeration is needed.
3. **Repair connectivity by averaging.** Lemma 3.1 proves `(1_T+F)/2` lies in the arborescence polytope even when `F` does not. For a set inducing a connected subtree of `T`, internal replacement flow can only leave through internal tree-removal nodes, so `F(E(S))<=|S|-1`. For a disconnected induced forest, `T(E(S))<=|S|-2`, while incoming-degree equations give `F(E(S))<=|S|`; their average again satisfies the rank bound. This two-case argument is the decisive bridge.
4. **Return to an actual tree without preserving per-sample q-bounds.** Round the circulation without bias inside its coordinatewise floor/ceiling intervals, discarding fractional q-dependent bounds during rounding. The integral replacement `B` may have directed cycles. The multigraph `T+B` nevertheless has two entering arcs across every rooted cut and exactly `2(n-1)` arcs. Decompose all its labeled copies into two actual directed arborescences and choose one fairly.
5. **Contract only the expectation.** This yields `E[1_T' | T] <= (1_T+q)/2`, hence `E[1_Tt] <= q+2^(-t)1_T0`. No stationary product law, total-variation mixing theorem, exact marginal estimator, or dynamic maintenance across a long packing history is required.
6. **Recover a cut using the already known reduction.** With upward-rounded `q` near `c/L`, a geometric guess `L<=lambda<4L/3`, denominator at least `16m`, and `t=ceil(log2(16n))`, the expected crossing count is below `35/24<3/2`. Every tree crosses at least once, so one crossing has probability above one half. Repeat, then use centroid recovery.

The circulation graph can be rebuilt in `O(m log^2 n)` size each iteration using tree-path range gadgets. There are only polylogarithmically many iterations, guesses and repetitions. Rebuilding is affordable because the mathematical iteration count changed; it would not be affordable once per event of the run's `O(m log N)` dynamic process.

A three-vertex sanity example exposes the missing distinction. Let `T={r->a,r->b}` and let the integral replacement be `B={a->b,b->a}`. `B` is cyclic and is not an arborescence. But `T+B` partitions into `{r->a,a->b}` and `{r->b,b->a}`. A failed replacement can be useful after controlled mixing and decomposition. This example illustrates Lemma 3.1; it does not assert that this integral `B` alone respects a feasible target `q`.

## Where the saved run came close

| Draft ingredient | Existing record | Missing connection |
| --- | --- | --- |
| One-respecting tree exposes an optimum | R0 `APPROACHES/A003-branching-packing.md`, L013/L014; R3 `APPROACHES/A006-tree-packing-reduction.md`, L006/L007 | This was already understood. Improving recovery again was not the main opportunity. |
| Packing as existence certificate | R3 L019 | Use existence to prove exchange-circulation feasibility instead of insisting on an efficient full packing construction. |
| Compact exchange paths | R3 `APPROACHES/A028-static-exchange-compression.md`, L034 | Add throughput constraints and interpret circulation as a fractional replacement; its static representation is useful even without a dynamic negative-cycle oracle. |
| Weaken the sampled object | R3 `APPROACHES/A051-undirected-tree-distributions-with-balanced-mean-tail-degrees.md`, L063 | The run relaxed directedness of individual samples, then sought a globally balanced law. The draft keeps actual directed trees between rounds and relaxes only an intermediate replacement. |
| Flow-assisted repair | R3 `APPROACHES/A055-routing-mean-deficits-through-unused-capacities.md`, L067/L068 | Its flow certifies supplied distribution marginals; the draft's flow *generates* a transition from one known tree. It removes the need to calculate those global marginals. |
| Generated cyclic structures | R1 A074/L328; R2 `APPROACHES/A074-circulation-cycle-contraction.md` and A080/L338 | Original-graph circulation, cycle-cover banks and completion of arbitrary generated forests impose different interfaces. They do not rule out repair relative to a supplied tree by half-step mixing. |

The transcript places the static compression audit at Sep 13 16:35 UTC and the turn to undirected mean degrees at 20:58 UTC. Within minutes of proving L063, the root was checking implicit Newton methods; later work concerned Gaussian Hessian sketches and entropy parameter range. This is direct evidence of the representation choice, not merely an interpretation of filenames.

Searches across all saved min-cut Markdown for half-steps, exchange circulations, averaging, unbiased rounding and two-arborescence decompositions did not locate the draft's complete transition lemma. That is a bounded statement about the saved records, not proof that the model never considered any related idea.

## Why progress did not compose

**A sufficient interface became the working definition of the problem.** R0 A003 already says a different one-entry witness family would suffice. R1's `AUDITS/2026-09-12T200228Z-audit-1-gpt-6-astra-cfdd50fb.md` explicitly recommends a packing-free random tree with small expected optimum-cut entry count. Yet the subsequent working interfaces repeatedly demand an all-price packing oracle, maintained approximate minimum-cost tree, supplied exact marginals, or a globally optimized product law. Each is sufficient; none is logically required by centroid recovery. The PDF's transition is a counterfactual route around those obligations.

**Local success was easier to measure than end-to-end progress.** R0's index names numerical optimization as the next action while still listing general reduction, exact output recovery, orientation normalization, history growth and phase-count gaps. R1 then resolves numerous conditional interfaces and family-specific repairs. In R3, A035-A045 continue a similar pattern: frozen-epoch monitoring, single-event repair and fixed-template rerooting are useful, but their composition lacks a global work charge. The later entropy branch repeats it with exact formulations, local curvature and bounded-step facts before a near-linear optimizer exists. This does not make those lemmas false; it makes their expected value to the target uncertain.

**Negative results sometimes acquired excessive strategic force.** R3 `audit_history/2026-09-13T174157Z-audit-3-kimi-code-k3-6e3c6013.md` recommends that the circulation branch A010-A018 be closed and never reopened. L025 only embeds general cut extraction in a specified original-graph circulation/residual-forest interface. The draft's auxiliary exchange circulation has a different state, invariant and output. Likewise, failure of simultaneous local exchanges does not exclude fractional replacement followed by averaging and decomposition. The saved author often states these scope restrictions correctly; the workflow should ensure they survive compressed summaries and audit recommendations.

**Audits tended to optimize the current framing.** The two R3 batches identify real whole-sequence work gaps, notation issues and repeated proofs. However, several assessments describe packing construction as the sole bottleneck and recommend increasingly precise tests inside the chosen framework. One Fable report calls A028 low value because it does not solve dynamic negative-cycle maintenance. In the draft, almost the same static representation supports a completely different transition. An audit needs a role explicitly responsible for searching across branches and changing the interface, alongside correctness and complexity checking.

**The record format amplified the problem.** R0 puts most work in one 1.4-million-character approach node. R3's requirement to append all mathematical work and make both nodes and PROVED fully self-contained produces long repeated reductions and duplicate proofs. R3's final `PROVED.md` alone is 904,102 bytes. The audits themselves recommend canonical proofs and shorter navigation. More text is not automatically more usable memory: the key bridge spans A006, A028 and A051, while much of the accessible frontier concerns descendants of one local obstruction.

The failure should not be attributed solely to pessimistic literature searches. Knowing that the theorem was not already in the inspected sources prevented false attribution. The harmful step would be treating that status as evidence against a new construction, or repeatedly checking the same status without a new question.

## Strength improvements to implement

These are general research behaviors, not a hidden min-cut answer embedded in every prompt.

1. **Keep a small live proof plan.** Record the exact target, the weakest sufficient output, the current critical lemma, its input/output/assumptions, all unresolved dependencies, a total running-time ledger, and the next discriminating test. Distinguish a historical approach edge from a proof dependency. Local RESOLVED status must not clear an unresolved dependency automatically.
2. **Require a synthesis checkpoint after stalled milestones.** Before adding another descendant to a blocked oracle, compare the weakest caller requirement with the actual requested output. Ask whether a single sample, expectation, additive slack, fractional state, delayed repair, averaging, rounding or decomposition would suffice. Search the existing positive lemma catalog for two or three compatible ingredients across different branches.
3. **Record both sides of every counterexample.** Store the exact failed quantifier/interface, a minimal witness, the assumptions used, and nearby possibilities it leaves alive. For this case, “arbitrary simultaneous exchange is invalid” should prompt a fractional/averaged repair test, while “residual forest extraction remains general” must not close all auxiliary circulation methods.
4. **Give independent agents complementary contracts.** One develops the current bridge; one tries counterexamples; one questions the representation or composes existing lemmas. Delegation should have a bounded deliverable and share a compact source map. Creating three agents to rediscover the same literature status is not useful diversity.
5. **Separate discovery from proof completion.** Test a proposed invariant on tiny instances before investing in data structures and precision. Once it survives, prove it, identify exact classical primitives, and then close arithmetic, representation, adaptive randomness and total-work obligations. Small-instance experiments can falsify a conjecture; passing them cannot prove it.
6. **Make audit recommendations actionable and scoped.** Require the author to accept, reject or defer each substantive recommendation with a brief mathematical reason, preserve the unresolved premise, and update the live plan. At least one audit should propose a concrete alternate interface or cross-branch composition; it should not be rewarded merely for more local counterexamples or restating that the target is open.
7. **Keep a solution-informed benchmark separate from general prompts.** A visible benchmark can ask the agent to reconstruct and audit the draft with attribution. A blind benchmark should withhold the PDF and this report, use fixed token budgets and seeds, and score whether it finds a valid weaker invariant and closes the proof. Training the prompt on the complete answer and then calling replay “autonomous discovery” would not measure the desired improvement.

For the min-cut task specifically, an informed continuation should make the PDF's Lemmas 2.2, 3.1 and 4.1 the first proof obligations, test the cyclic-replacement example, verify unbiased rounding on the compressed graph, and then compose with the already established recovery theorem. This is an actionable experiment inspired by the supplied solution; it is not independent rediscovery.

## Independent check of the supplied draft

The draft's new argument is the exchange-circulation feasibility/half-step/transition chain, together with its composition and finite implementation. The existence of branchings, fast flow, unbiased flow rounding, and one-respecting recovery are imported primitives, not new discoveries of this review.

The following primary sources support the interfaces used:

- [Tarjan, *Edge-disjoint spanning trees and depth-first search*](https://collaborate.princeton.edu/en/publications/edge-disjoint-spanning-trees-and-depth-first-search/) explicitly concerns **two rooted spanning trees in a directed graph**, with almost-linear time. Its title must not be mistaken for an undirected-tree decomposition. The [Stanford technical-report version](https://i.stanford.edu/pub/cstr/reports/cs/tr/74/455/CS-TR-74-455.pdf) likewise states a near-linear directed result. In the draft's `2(n-1)`-arc union, two spanning arborescences necessarily use every copy.
- [Kang and Payor, *Flow Rounding*](https://arxiv.org/abs/1507.08139) provides random integral rounding preserving each edge's expectation. The supplied draft additionally gives a self-contained dyadic Euler-tour implementation, avoiding reliance on a general numerical rounding oracle.
- [Chen et al., *Maximum Flow and Minimum-Cost Flow in Almost-Linear Time*](https://arxiv.org/abs/2203.00671) covers exact directed flows with polynomially bounded integral demands, capacities and costs. Lower-bound circulation feasibility reduces to that interface with polynomially bounded auxiliary demands.
- [Cen et al., *Minimum Cuts in Directed Graphs via sqrt(n) Max-Flows*, Theorem 12](https://arxiv.org/pdf/2104.07898) supplies the one-respecting recovery primitive. The draft's connected-sink proof spells out the relevant centroid argument.

Checks performed on the draft:

- Re-derived the connected/disconnected-set split in Lemma 3.1 and the Hall condition in Lemma 2.2. In particular, a union of paths in a tree contains every tree edge internal to each of its connected vertex components.
- Checked that rounding discards q-dependent bounds but preserves selected throughputs in `[0,1]`, conservation and conditional expectations. Keeping q as a per-sample upper bound would break the argument.
- Checked that exact range compression preserves throughput feasibility by downward-path decomposition; expansion is existential and must not be materialized. Rounding occurs on the compressed network.
- Checked the contraction recurrence, `35/24` crossing bound, geometric guesses and dyadic denominator accounting. Successive trees need not be independent; complete sampler repetitions use fresh randomness.
- Inspected and reran the pre-existing, ignored `output/analysis/check_half_step.py`, then preserved an analysis-only version as [tools/check_mincut_half_step.py](../tools/check_mincut_half_step.py), with `--max-n` defaulting to 4. Its `--max-n 5` output matched the saved results byte-for-byte. It exhausts all rooted trees and one-parent replacements on complete loopless digraphs with no root-entering arcs for `n=2..5`: 32,445 pairs, 22,939 exchange-feasible pairs, 341,359 rooted-cut checks, and 22,939 successful exhaustive two-arborescence partitions. Of the feasible replacements, 7,048 are cyclic. These checks independently exercise the matching/union criterion, but do not test fractional circulation, rounding, range compression, parallel-arc identities, large inputs or running time. The tool is not loaded into author prompts.

No error was found in these checked arguments. Several qualifications must nevertheless be made explicit before claiming the exact original run statement:

1. **Zero cuts and global reduction.** The PDF assumes positive capacities and rooted reachability and leaves the global reduction implicit. Delete zero arcs for reachability, find a sink SCC if the positive graph is not strongly connected, and return its zero outgoing cut. Otherwise run the rooted algorithm in both orientations and translate the chosen side correctly.
2. **Use the master input size.** The run allows capacities up to `(n+m)^C`, arbitrary parallel arcs, and failure at most `(n+m)^(-3)`. The PDF states `U=n^O(1)` and an `n`-based failure bound. Set `N=n+m`, use `O(log N)` repetitions and per-call failure budgets based on N, and charge all inherited integer capacities and rational guesses in `O(log N)`-bit words. A claim of `1-n^-a` alone does not imply `1-N^-3` if arbitrarily many parallel arcs are allowed. These are straightforward parameter adaptations, but they must be written rather than assumed.
3. **Preserve distinct copies.** Parallel original arcs and the two copies of an arc in `T+B` must remain identifiable through decomposition and projection.
4. **Validate the imported algorithmic interfaces.** An existence theorem for branchings is not a decomposition algorithm. A decomposition into ordinary undirected trees is not enough to construct the next exchange network. Flow feasibility, capacity magnitudes, total graph size, finite arithmetic and failure amplification must all be charged in the final proof.

The PDF therefore offers a credible and unusually direct route for a focused continuation. The strongest lesson is that the existing archive contained useful ingredients, but the research process needed an explicit opportunity to recombine them around a smaller invariant.

Reproduce the finite check and the latest run's stream analysis from the repository root:

```sh
python3 tools/check_mincut_half_step.py --max-n 5
python3 tools/analyze_run.py runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global
```

The analysis tool also accepts several run directories in one invocation. Its usage counters are observed provider telemetry with explicit coverage limits, not a bill. The inventory's item counts use completed-item identities; the analysis tool additionally separates root/subagent scope and handles counter resets for token measurements. A fresh full-strength blind research run is still needed to measure whether the implemented workflow changes improve theorem discovery per token.
