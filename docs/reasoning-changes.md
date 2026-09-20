# Research workflow changes

These changes improve the search procedure and prevent an unreviewed candidate
from being reported as approved. They do not establish that the prover can solve
directed minimum cut independently. That requires a new, isolated run and a
separate mathematical review of its result.

## Reasoning strength

The managed author's records remain `INITIAL_PROMPT.md`, the `APPROACHES/` DAG,
and `PROVED.md`, with controller-managed audit directories. Existing saved initial
instructions remain authoritative; the changes do not rewrite historical runs.
New managed runs receive the revised instructions automatically. To apply new
research instructions to an existing run, use an explicit user-authorized
instruction update, preserving its statement and original record.

`APPROACHES/index.md` now has a compact working frontier before the full DAG table.
Each active route records proved ingredients, one exact missing bridge, and its
next discriminating test. Two focused attempts that do not reduce the same bridge
trigger a bounded probe of a structurally different mechanism. This is an
instruction to reconsider the route, not a time limit on a difficult proof.
Several implementations of the same unavailable primitive do not count as
independent routes.

The author must compare the interface it actually needs with stronger interfaces
it has chosen: one object or sample versus a whole explicit family; expectation
versus uniform guarantees; existence versus construction; static computation
versus dynamic maintenance. After a negative result it must record the exact
quantifiers, premises, representation, and failed mechanism. An obstruction for
one construction on the original instance cannot close every use of a similarly
named construction on an auxiliary instance. The next question is which weaker
invariant survives and whether existing lemmas from different branches compose.

Every important reusable result needs an exact contract, dependencies, and
applicable computational and probability guarantees. External results need a
primary source and precise theorem location, with the hypothesis match checked.
Oracle calls, total input volume, precision, sampling, rebuilding, actual inspected
records, and adaptive randomness must be charged across the whole execution.
An existence proof, local bound, or conditional reduction cannot stand in for an
efficient complete algorithm. Conjectures and experimental evidence stay distinct
from proved results.

Consequential audit suggestions are tracked as accepted, tested, deferred, or
declined with a reason and evidence. This keeps a promising weaker interface from
disappearing through compaction. The audit emphasizes the decisive mathematical
gap, a concrete next test, and only consequential readability defects. Its scoped
negative conclusions remain advisory and must be checked by the author.

No directed-cut construction, PDF lemma, or solution hint is embedded in the
default prompts. These are general research instructions suitable for a blind
rerun on a fresh workspace.

## Completion safeguards

The earlier critic transition accepted the latest edited proof when the configured
review-round limit was reached. The revision itself had not received an unchanged
acceptance. The new transition sends it back to the author with an explicit
unverified-revision objection. A critic rejection also returns to the author.
The round limit bounds consecutive critic calls; it is not a proof criterion.
The overall workflow budget still bounds continued research.

Only a `pass` returning the exact candidate string sets `proof_verified=true` and
publishes the proof as workflow output. Even whitespace changes require review of
that exact candidate. The preserved candidate is not stripped after acceptance.
Author work and new critic reviews clear the flag and stale output first. Failure
does not invoke the document writer.

The document writer now returns `{verdict, latex, bugs}`. `preserved` requires
faithful exposition with no unresolved substantive issue. The writer can reorganize
the supplied explanation and fix LaTeX but cannot create a missing proof, import a
new theorem, or silently correct a mathematical defect. `unresolved` preserves any
partial LaTeX in `latex-source.tex`, records the objections, and stops before
compilation or a final-result event. No additional model call is introduced.

These gates enforce response consistency and workflow routing. An LLM's acceptance
or fidelity declaration is not formal verification, and the code does not prove
semantic equivalence of Markdown and LaTeX. Independent proof review remains
necessary for a novel mathematical result.

## Cost changes within the prompts

The frontier guides selective retrieval: read the permanent instructions, then the
frontier, then the active node and necessary lemma dependencies. The complete
proof records remain available. Search headings and IDs before opening long files;
expand retrieval whenever checking a hypothesis or contradiction requires it.
An ordinary continuation can reuse unchanged initial instructions already in
context; initial start, actual resumption, compaction, a changed file, or missing
instructions still require a read. The initial research prompt is longer because
it now specifies these research contracts; it is not claimed to be a shorter prompt.
Store substantive arguments and decisions without repeated narrative or duplicate
full proofs. Helpers get bounded questions and relevant contracts. Audits use
controller-provided change metadata and complementary focus, with periodic full
reassessment; summaries and metadata never certify correctness.

The writer checks fidelity once across the complete final argument, then revisits
affected passages when subsequent edits warrant it. It does not restart every
unchanged check after a cosmetic edit or initiate a new literature survey.
These policies retain the strong author and independent critic; token savings
have not been measured in a controlled new proving run.

## Validation

Offline regression coverage exercises exact unchanged acceptance, whitespace
changes, edited-review exhaustion, author repair feedback, preservation of the
saved candidate, failed research stopping the pipeline, writer objections stopping
compilation, and incompatible writer verdict/bug combinations. Existing LaTeX
compilation and prompt-default tests continue to exercise the normal successful
path. These tests check controller behavior; they do not measure discovery ability.
