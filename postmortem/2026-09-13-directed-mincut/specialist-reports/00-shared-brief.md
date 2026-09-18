# Shared brief for trajectory analysis agents

## Paths
- Run directory (the failed attempt): /Users/yonggangjiang/Downloads/tcs-prover/runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global
  - INITIAL_PROMPT.md (author instructions + statement), APPROACHES/index.md (DAG of 60 nodes A001–A060), APPROACHES/A0xx-*.md, PROVED.md (73 lemmas L001–L073, 9558 lines; use grep -n "^## L0" to locate), AUDITS/ (3 reports from batch 2), audit_history/ (3 reports from batch 1), partial-output.md, pause.json, transcript.jsonl (65MB raw, do not read whole).
- Harness: /Users/yonggangjiang/Downloads/tcs-prover/workflows/author_critic.yaml (author + critic prompts), /Users/yonggangjiang/Downloads/tcs-prover/workflows/research_audit.yaml (audit prompt), /Users/yonggangjiang/Downloads/tcs-prover/workflow_runner.py, README.md.
- Independent solution: PDF text at SCRATCH/pdf.txt (11 pages, "Sampling Arborescences for Exact Directed Minimum Cut").
- Pre-extracted transcript views in SCRATCH (= /private/tmp/claude-503/-Users-yonggangjiang-Downloads-tcs-prover/62d437f4-1343-4f50-b460-a8439c134571/scratchpad):
  - narrative.txt (10009 lines, time-ordered: ROOT/SUB messages, reasoning summaries "THINK", FILE changes, SUBAGENT events, WEBSEARCH, "*** CONTEXT COMPACTION ***" markers). Times are UTC HH:MM:SS on 2026-09-13.
  - subagents.txt (subagent events + 125 subagent final results), filechanges.txt, control_events.txt, websearches.txt, commands.txt.

## Established timeline facts (verified from transcript)
- Fresh run. Root author model gpt-6-astra, effort ultra, with Codex multi-agent subagents (named literature_status, structural_approaches, flow_reduction, approximation_proofs, audit_cleanup, batch_supersets).
- Segment 1: 15:06–17:00 UTC (turns of 9, 17, 38, 34, 13 min). Paused for audit batch 1 (17:11–17:49). Segment 2: 17:49–19:49 (turns 28, 40, 51 min; last interrupted for audit). Audit batch 2 (19:49–20:13). Segment 3: 20:13–22:06, one 113-min turn that ENDED WITH A CODEX USAGE-LIMIT ERROR ("You've hit your usage limit... try again Sep 20"), not with the 168-hour budget. So the run was cut off after ~6 hours of author time.
- 30 context compactions of the root thread (plus subagent compactions). After each, the harness sent: "Context was compacted. Read INITIAL_PROMPT.md now and follow its instructions. Recover the relevant saved context using its memory layout, preserve the records, and continue the same goal."
- One user steer at 16:39: "You missed several new added node in the approach index.md".
- Within 2 minutes (15:07:58) the author found an Aug 2026 paper calling exact almost-linear directed mincut "a major open problem" and recorded it as A001 (RESOLVED).
- By 15:11 the author had the conditional reduction: L006 (centroid tree-search recovery, O(N log n) total flow input) + L007 (packing of mass ≥ 2ρ/3 gives one-crossing prob ≥ 1/2). This is IDENTICAL to the independent solution's Sections 1.1 and 7. From then on the missing piece was always "efficiently sample a tree from a near-feasible arborescence packing".
- 209 web searches, 1093 shell commands, 79 file-change events writing ~13k lines; PROVED.md 900KB, APPROACHES 1.5MB.
- Statuses at end: 33 RESOLVED, 9 CLOSED, 8 BLOCKED, 10 ACTIVE; none resolves the statement. Many lemmas are lower bounds / obstructions against the author's own sub-proposals (L009, L010, L014, L020, L021, L027, L029, L030?, L036, L037, L038, L056, L059, L060, L062, L065, L066, L069, L070).

## The independent solution (PDF) decomposed into ingredients
Rooted formulation: root r, cut = incoming boundary δ⁻(S) of sink side S ⊆ V\{r}; λ = min. (Agent used the mirror: out-arborescences/in-arborescences and δ⁺; equivalent by reversal.)
I1. One-respecting tree ⇒ O(log n) maxflows recover the cut (centroid recovery, Cen–Li–Nanongkai–Panigrahi–Saranurak). [Agent HAS this: L006.]
I2. It suffices that a random arborescence T has E|T ∩ δ⁻(S*)| < 3/2, i.e. marginals p_e = P[e∈T] with p(δ⁻(S*)) < 3/2; e.g. p_e ≤ α c_e/λ, α<3/2. [Agent HAS this: L007, and L063 generalizes to marginals of undirected trees.]
I3. Edmonds' theorem ⇒ a point x of the arborescence polytope A_r with x_e ≤ c_e/λ EXISTS (fractional packing). Used ONLY existentially. [Agent HAS existence: L019, but kept trying to CONSTRUCT/maintain the packing.]
I4. KEY IDEA: do not construct the packing. Maintain ONE arborescence T and repeatedly replace it by a random T' with E[1_{T'} | T] ≤ (1_T + q)/2, where q ≈ c/L is a target marginal vector. After t = O(log n) steps E[1_{T_t}] ≤ q + 2^{-t} 1_{T_0}, so the crossing expectation of any fixed cut is < 3/2.
I5. The update is computed as a feasible CIRCULATION with lower/upper bounds on an "exchange network": nodes e_in/e_out per arc; tree arcs get throughput in [1−q_e, 1], non-tree arcs [0, q_f]; arcs e_v^out → f^in for non-tree f with head v (same-head, preserves indegree); f^out → e^in for tree arcs e on the fundamental path P_T(f). Throughput z defines F (F_e = 1−z_e on tree arcs, z_f on non-tree). Feasibility (Lemma 2.2) follows from existence of x ∈ A_r with x ≤ q via a Hall-condition / transportation argument on fundamental paths.
I6. HALF-STEP LEMMA (Lemma 3.1): (1_T + F)/2 ∈ A_r (arborescence polytope: x ≥ 0, x(δ⁻(v)) = 1, x(E(S)) ≤ |S|−1), even though F itself may contain cycles. Proof: for T[S] connected, exchange flow from internal non-tree arcs lands on internal tree arcs; for T[S] disconnected, |T∩E(S)| ≤ |S|−2 and F(E(S)) ≤ |S|, so the average is ≤ |S|−1.
I7. UNBIASED ROUNDING of the circulation (Kang–Payor flow rounding, or dyadic Euler-tour rounding, Appendix A) gives an integral circulation Z with E[Z]=z, floor/ceil bounds ⇒ B ∈ {0,1}^E with one incoming arc per vertex, E[B]=F.
I8. TWO-ARBORESCENCE DECOMPOSITION: (1_T + B)/2 ∈ A_r ⇒ multigraph T+B (2(n−1) arcs) has every root-excluding in-cut ≥ 2 ⇒ Edmonds' disjoint branchings theorem gives 2 arc-disjoint arborescences partitioning T+B (Tarjan's algorithm). Pick one uniformly ⇒ E[1_{T'} | T,z,Z] = (1_T + B)/2.
I9. COMPRESSION: exchange network has Θ(mn) fundamental-path incidences; heavy-light decomposition + segment tree gives an exact O(m log² n)-arc network (Lemma 5.1). [Agent HAS an analogous compression: L034 / A028 (binary-lifting blocks, O(m + n log n)) — but used it for weighted exchange WALKS / negative cycles for a dynamic tree oracle, not for a flow.]
I10. Target q_e = min{1, ⌈µ c_e/L⌉/µ} for a geometric guess L of λ (O(log(mU)) guesses), dyadic for O(log m)-bit arithmetic; if L ≤ λ every circulation is feasible; infeasible guesses are discarded; every returned candidate is a valid cut so min over all is exact w.h.p.
I11. Running time: O(log(mU) log² n) exact maxflows (Chen et al. almost-linear) on O(m log² n)-size networks ⇒ m^{1+o(1)}.
The new conceptual content is I4–I8 (a "marginal contraction" Markov-chain-style sampler using one circulation + rounding + Edmonds decomposition per step). Everything else the agent already had or is textbook.
