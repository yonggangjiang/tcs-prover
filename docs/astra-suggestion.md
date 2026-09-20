# Astra suggestion: implementation and evaluation

This branch implements two separate proposals, based on the directed min-cut
trajectory and xean at commit `ded54a685d628e5e7ee9eded9be7fb63211f0ef0`.
The baseline is TCS Prover commit `ff86bdc`. The reference PDF and historical
research folders are not modified or injected into the default author prompt.

## 1. Strengthen research and verification

The [trajectory review](mincut-trajectory-review.md) explains the missed
mathematical connection with exact run paths and lemma IDs. The
[reasoning proposal](reasoning-changes.md) describes the implemented changes.

The central failure was overcommitting to stronger sufficient interfaces:
constructing or maintaining a packing, optimizing a full distribution, and
estimating marginals. The supplied draft instead updates one actual tree using
an auxiliary circulation, a half-step invariant, unbiased rounding and a
two-arborescence partition. The run already had closely related static exchange
compression and cut recovery. Counterexamples and audit summaries sometimes
closed a broader family than their premises justified.

Implemented changes:

- Put the exact remaining bridge, diverse routes, decisive tests and audit
  decisions in a compact frontier inside `APPROACHES/index.md`.
- Compare the caller's weakest sufficient interface with stronger intermediate
  requirements; reconsider a route after repeated unchanged obstacles.
- Record counterexample scope, surviving invariants, source contracts,
  dependencies and total complexity/probability obligations.
- Require byte-identical critic acceptance. A revised proof at the round limit
  returns to the author for fresh review. Exhaustion is not approval.
- Restrict the writer to faithful exposition. A declared mathematical gap stops
  publication, preserving the objection and any partial LaTeX.

The PDF itself is a candidate draft. Its positive-capacity rooted formulation
also needs explicit zero-cut handling, orientation reductions and master-size
probability/bit-complexity bookkeeping to match the run's full statement. The
review and finite checker provide evidence, not formal verification.

## 2. Reduce avoidable token use and improve measurement

The [efficiency proposal](efficiency-review.md) separately gives measured
historical usage, xean source references, implemented adaptations and deferred
ideas. The principal adaptations are durable local contracts, selective access
to necessary support, independent verification of exact candidate versions, and
explicit measurement gaps.

- Keep Astra and Ultra as the defaults. Use Standard service by default; Fast
  remains selectable and saved explicit settings are preserved. This is a
  service-tier choice, not a reduction of reasoning effort or token count.
- Preserve substantive mathematical work without repeatedly copying complete
  proofs or narrating unchanged decisions. Retrieve through the frontier and
  dependency links; expand reads whenever correctness requires it.
- Supply fresh auditors with bounded file-change metadata after the author
  pauses, plus different mathematical focuses. They still have access to all
  research files. Their baselines contain hashes, sizes and counters, never
  previous verdicts or peer reports.
- Review the full route portfolio initially and every fourth successful review
  for each slot; changed tasks/models/prompts and oversized change sets also
  trigger full review. Failures retain the last successful baseline. Audits are
  not skipped merely because records are unchanged.
- Preserve available audit provider usage on successful, failed and cancelled
  calls; unknown usage remains unknown. Analyze historical counters with
  `tools/analyze_run.py`, separating cache hits, counter resets and subagents.

`context.fullEvery: 1` requests full audits every time. `context.maxPaths` limits
only the metadata list, not access to proofs. Custom audit prompts without the
two context/focus markers remain byte-for-byte unchanged. Existing cache-prefix
settings, the persistent author conversation, independent critics and complete
proof records remain available.

## Adoption

For the full research workflow, enable **Manage research files** when starting
a fresh run. The home-screen simple mode intentionally keeps its short prompt
and does not receive the frontier instructions or scheduled research audits.
New managed runs use the revised YAML. Saved runs retain their recorded initial
instructions and custom role prompts; they are not silently migrated.

For an existing run, explicitly guide the author to use the new general research
protocol while preserving its exact statement and historical records. Applying
the supplied PDF as guidance is a separate, solution-informed experiment and
must be labeled as such. The original run has not been resumed by this change.

## Validation and a fair follow-up experiment

Offline tests cover critic routing, faithful-writer declarations, interruption
and resume, audit baseline invalidation and failure, bounded context hints,
provider usage capture, and counter accounting. They do not measure mathematical
discovery or prove that a novel result is correct.

A live evaluation should use these conditions:

1. Use independent clean workspaces for baseline and revised workflows, the same
   exact task, the same models/efforts, and the same service tier. Exclude the
   PDF, this analysis, the old notebooks and any solution-specific hints from
   both workspaces and prompts. Online search can still disclose a solution;
   retain source provenance and report discovery separately from retrieval.
2. Fix equal research token allowances, audit settings and completion criteria.
   The current harness enforces elapsed time; it does **not** implement a new
   hard aggregate token budget across delegated agents. Use measured token
   checkpoints and manual/supervised stop for this experiment, or implement a
   tested provider-wide budget before claiming strict budget parity.
3. Run several independent trials, plus other held-out problems. Compare
   uncached input, cached input, output/reasoning and missing usage separately.
   Price only with the applicable provider plan; do not infer dollars from
   transcript size. Report successful exact proofs and independently verified
   critical-bridge progress per measured token, along with failures.
4. Ablate reasoning changes and efficiency changes separately. Require a fresh
   independent review of the exact final proof, including total running time,
   finite arithmetic, orientation and parallel-arc cases, and probability
   amplification. If selective retrieval misses a dependency, increase full
   review frequency and record the regression.

No paid live proving experiment was launched as part of this implementation.
The changes remove identifiable waste and controller defects; lower cost and
stronger autonomous discovery remain hypotheses to test, not reported outcomes.

Validation result (2026-09-20): all 271 tests passed in the isolated branch
checkout; JavaScript syntax and `git diff --check` passed. The analysis-only
checker passed all 32,445 tree/replacement pairs through five vertices.
