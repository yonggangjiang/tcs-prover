# Efficiency review of the directed min-cut campaign

Reviewed on 2026-09-20. This document separates measured overhead, implemented changes, and experiments that still need evidence. Mathematical diagnosis belongs in the accompanying min-cut postmortem. No measured saving or preservation of mathematical success rate is claimed without a new controlled run.

## Scope and reproduction

The four research transcripts in the directed min-cut lineage were scanned completely, one JSONL record at a time. This includes the original attempt, migrated workspace, short prepared restart, and latest attempt. The earlier reachability/SCC runs and unrelated paper-formatting run were excluded. The brief statement-review-only min-cut run is also excluded from these research totals; these are not all-time account totals.

Reproduce the table without making model calls:

```sh
python3 tools/analyze_run.py \
  runs/2026-09-11_20-36-05_design-and-analyze-an-algorithm-for-exact-global \
  runs/2026-09-12_17-24-47_directed-mincut-migrated \
  runs/2026-09-13_15-00-00_directed-mincut-prepared \
  runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global \
  > /tmp/mincut-usage.json
```

The script emits aggregate counters and identities, not transcript prose or command contents. It accepts individual run folders or transcript files and does not recursively include unrelated runs. Run artifacts are deliberately untracked.

## What the records actually measure

| Research run | Input tokens | Cached input | Uncached input | Output tokens | Reasoning subset of output | Recorded cache hit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sep 11 original | 348,537,263 | 331,543,296 | 16,993,967 | 6,455,491 | 4,661,907 | 95.124% |
| Sep 12 migrated | 511,828,100 | 495,279,104 | 16,548,996 | 3,326,206 | 1,789,111 | 96.767% |
| Sep 13 prepared | 11,780,667 | 11,310,848 | 469,819 | 111,036 | 68,161 | 96.012% |
| Sep 13 latest | 267,945,496 | 258,896,896 | 9,048,600 | 1,830,446 | 910,754 | 96.623% |
| **Total observed** | **1,140,091,526** | **1,097,030,144** | **43,061,382** | **11,723,179** | **7,429,933** | **96.223%** |

These are observations from Codex app-server counters, not a provider bill. Cached input remains part of input. Reasoning is already included in output and must not be added again. The combined recorded input plus output is 1,151,814,705 tokens.

The distinction between root and child work is substantial:

| Scope | Input | Uncached input | Output | Reasoning subset |
| --- | ---: | ---: | ---: | ---: |
| Author/root | 447,454,313 | 11,424,873 | 2,935,111 | 1,279,984 |
| Subagents | 692,637,213 | 31,636,509 | 8,788,068 | 6,149,949 |

Subagent counters account for about 60.8% of observed input and 75.0% of output across the lineage. On the latest run they account for about 70.5% of input. This supports improving delegated task boundaries and shared retrieval before lowering the author model's reasoning effort. It does not establish that those agents were unproductive.

### Counter semantics and missing coverage

The 12,559 usage-update records contain 2,802 unchanged cumulative snapshots and 19 counter resets. Counting each snapshot as a fresh response would inflate usage drastically. Keeping only the last snapshot would undercount resumed work: the latest author's three counter epochs end at 23,693,361, 27,450,919, and 28,419,733 input-plus-output tokens. Its actual recorded sum is 79,564,013, not the last 28,419,733. Child counters also reset on resume.

The analyzer sums monotone changes within each thread and adds the new counter on a detected reset. An omitted field retains its last observed baseline within an epoch; otherwise its reappearance would count the same usage twice. It reports author and subagent counters separately and assumes these scopes represent distinct recorded usage; it does not infer an undocumented parent billing rollup. A first counter can include earlier usage outside the file. An unobserved reset can be missed. Those limitations matter for arbitrary imported transcripts, even though this lineage begins fresh author sessions and contains visible resume/reset boundaries. Unattributed interleaved native CLI usage is retained separately and excluded from totals.

All 24 completed research audit reports in the migrated/latest transcripts lack provider usage records: 18 in the migrated run and six in the latest run. Failed or interrupted provider calls may also be unmeasured. Thus even correct author/subagent counting cannot recover the full campaign bill. Existing “Author cache usage” status messages show the last observed request when a goal turn finishes; they are not a complete cost ledger and are not added to native counters. Subscription quota consumption, API token charges, and provider-reported estimated dollar costs are different quantities. The analyzer returns `cost_usd: null` rather than inventing prices.

New `audit_usage` events are reported separately with request identity, provider, coverage, outcome, and raw usage. Completed audit events join these records by `requestId`; an empty dictionary or an unknown field is not measured coverage. Coverage requires recognized nonnegative integer counters or a finite nonnegative reported cost. In particular, Claude cache counters and Codex cached-input counters have different conventions; the tool preserves them instead of silently summing them. A failed/cancelled request with no usage remains explicitly missing. Provider-reported estimates are evidence, not automatically an invoice.

### Context, retrieval, and storage

The lineage has 165 completed context-compaction items, 4,884 completed command-execution items, and 53,181,352 characters of command output. Exact repeats of outputs of at least 500 characters contribute another 6,243,607 characters after their first occurrence. Those repeated bytes are a retrieval diagnostic, not an estimate of tokens saved: re-reading can be necessary after compaction, and repeated output can be a legitimate verification.

The exact shell command `cat INITIAL_PROMPT.md` occurs 340 times: 25 in the original run, 242 migrated, zero in the prepared run, and 73 latest. These counts exclude combined commands and alternative quoting. The original contract expressly required reading it after compaction and resumption. This is a reason to shorten and stabilize the operational context and resume instructions, while keeping the complete problem statement available. It is not evidence that all re-anchoring should be removed.

The four transcripts occupy 1,499,470,974 bytes over 649,242 records. The original transcript alone is 1,281,650,101 bytes; `turn/diff/updated` records contribute 1,056,658,497 serialized characters. Most of that large file is accumulated diff telemetry. It does **not** follow that the entire 1.28 GB was repeatedly sent to the model. Compressing or replacing diff snapshots would chiefly improve storage, loading, and inspection. It should be evaluated separately from inference token savings.

All four saved research settings use Astra at ultra effort with fast service mode. The latest saved audit setting is one hour even though earlier settings and the user's description say two hours; the transcript nevertheless contains two completed three-auditor batches. Actual events, not the final configuration alone, determine historical counts.

## What xean contributes

The source inspected was `chaoxu/xean` main at commit **`ded54a685d628e5e7ee9eded9be7fb63211f0ef0`**. The web repository was checked and its code, workflow, provider accounting, recovery paths, and efficiency tests were inspected locally. This is an architectural comparison, not a measured head-to-head benchmark. Its important lesson is to preserve expensive reasoning as reusable evidence and choose subsequent context deliberately.

| Mechanism in the inspected xean revision | Why it matters here | Adaptation status |
| --- | --- | --- |
| A fresh Explorer request has the task, guidance, all note metadata, and coordinator-selected full notes plus their support closure; it does not inherit every old reasoning transcript. [Workflow request construction](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/workflow.ts#L245-L284) | Prevents the archive becoming default working context while leaving all discoveries retrievable. | Apply the retrieval principle in author/audit instructions; automatic fresh-thread turnover is deferred. |
| Successful verdict reports are omitted from working prompts; failures and uncertainty retain their explanations. [Prompt note construction](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/pi-roles.ts#L155-L171) | Carry the established claim and provenance; spend attention on unresolved obstacles. | Preserve complete proofs on disk and use concise navigation in prompts. No proof is deleted or automatically declared correct from its summary. |
| Verification reads deduplicated support closures, fits batches by text length, and visits shared ancestors once. [Verification window](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/workflow.ts#L169-L203), [closure traversal](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/support.ts#L7-L31) | Shared lemmas need one dependency record, not repeated informal derivation in every branch. | Dependency-aware author records and audit hints; a typed immutable note database is deferred. |
| Source verification gets exact externally required results without every supporting proof, and can reuse previously inspected passages with completed-call provenance. Empty external-premise sets need no source model call. [Source packet](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/pi-roles.ts#L480-L545), [passage reuse](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/pi-roles.ts#L922-L1006) | Repeated literature status searches and theorem checks can use exact claims, hypotheses, and source passages already established. Applicability still must be checked. | Prompt guidance now favors precise reusable evidence; automatic source-result caching is deferred. |
| Immutable notes and candidate-specific verdicts determine verified/dead/accepted status; failed support invalidates dependents. [Projection](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/projection.ts#L70-L110) | The safe unit of reuse is an exact claim with its dependencies and verified hypotheses. An old advisory audit of a similar idea is insufficient. | Preserve TCS's candidate checkpoints and tighten critic acceptance to the exact submitted text; do not reuse advisory conclusions as proof verification. |
| Several substantive submissions can continue in the same context; the submission gate reserves output room and stops at a response/context limit. [Role settings](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/pi-roles.ts#L90-L113), [context budgeting](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/src/pi.ts#L908-L962) | Avoids both stopping before useful mathematics and growing one thread indefinitely. | Bounded research milestones are adopted as guidance. Hard response limits and forced thread resets need a held-out quality comparison. |
| Stable role/system cache keys are separate from changing task material. [Provider call construction](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/pi-roles.ts#L548-L590) | Preserve stable prefixes while changing only the requested work. | TCS already has a stable structured-request cwd and compaction prefix configuration; preserve these. The 96.223% observed hit rate argues against treating cache absence as the main problem. |
| Accounting exposes unmeasured requests and unpriced native Codex calls. [Accounting](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/packages/solve/accounting.ts#L6-L33), [spend aggregation](https://github.com/chaoxu/xean/blob/ded54a685d628e5e7ee9eded9be7fb63211f0ef0/src/pi.ts#L707-L750) | Missing evidence must not appear as a low-cost success. | Implemented offline observed-usage analysis and separate audit usage/coverage events. |

Do not copy xean's architecture wholesale. Its four verification dimensions, coordinator calls, and reconstruction attempts themselves cost tokens. They address proof reliability but are not automatically cheaper than TCS. Its `replayReasoning: false` option changes what a model receives; adopting that without evidence could reduce reasoning quality and disrupt caching. Its low-level projection/closure tests establish computational efficiency, not reduced mathematical cost. These are hypotheses for experiments, not proven savings for this campaign.

## Efficiency changes implemented in this branch

1. **Make spend inspectable.** `tools/analyze_run.py` scans large transcripts once, handles repeated cumulative counters and visible resets, separates root/subagent usage, exposes historical audit coverage gaps, and reports repeated retrieval and storage diagnostics. It makes no provider calls and assumes no pricing. New audit telemetry retains provider-native usage for completed, failed, and cancelled calls where reported.
2. **Focus independent audits on changes, with regular full reviews.** A frozen file-hash snapshot identifies changed research material while the author is paused. All selected auditors share that snapshot, with a separate baseline advanced only after each auditor succeeds, and complementary review focuses. They can inspect dependencies and unchanged files. The initial review, changed task or audit identity, and every fourth successful review restore broad review. Hints list at most 60 paths by default; larger changes also request a full review. Marker-free custom prompts remain unchanged. This is guidance, not exclusion or permission to skip reasoning. Auditors still do not see peer audit reports, and final proof criticism is separate. There is no automatic suppression of scheduled audits solely because hashes are unchanged: a fresh independent perspective can still matter on a stalled proof.
3. **Make standard service mode the default for new settings.** Retain the selected author model and reasoning effort. Saved speed choices remain intact and fast mode remains available. This changes the latency/cost preference without substituting a weaker mathematical model; no dollar saving is asserted because this review does not have account billing data.
4. **Reduce redundant research work through the author contract.** Record concise branch summaries, exact reusable premises, specific counterexamples, and the next decisive test. Delegate disjoint mathematical questions with useful completion criteria, reuse recorded evidence, and retrieve detailed nodes as needed. Keep complete mathematical arguments and counterexamples; stop requiring transcripts of every discarded thought. These instructions overlap with the strengthening changes, but their efficiency effect should be measured separately.

## Further optimizations proposed separately

| Priority | Proposal | Quality safeguard and measurement |
| --- | --- | --- |
| Next | Add a persistent claim/source registry keyed by exact statement, hypotheses, source passage, and content hashes of proof dependencies. | Editing any dependency invalidates reuse. Compare repeated source calls and independently verified results per uncached/output token. Do not key on theorem title alone. |
| Next | Introduce an explicit selection of active nodes and their dependency closure at voluntary fresh-thread boundaries. | The exact task, unresolved objections, failed constructions, and retrievable archive survive. First compare against existing compaction on held-out problems; forced resets are not implemented. |
| Next | Measure delegated task yield: a new lemma, a verified counterexample, or an exact unresolved gap per task, with per-thread usage. | Keep independent audits and strong reasoning. Avoid duplicate derivations across workers; do not blindly cap exploration by file count or token count. |
| Later | Compress/archive transcript deltas and keep a small incremental inspection index. | Preserve raw evidence or lossless recoverability, crash recovery, and final diffs. Validate disk/load savings separately from model tokens. |
| Experimental | Route purely mechanical formatting/index maintenance to deterministic code or a cheaper model. | Keep novel proof search and acceptance checks at their current strength. Compare corruption/error rates and net savings including repair calls. No automatic model downgrade is implemented. |
| Experimental | Adaptive audit frequency based on a research milestone, changed load-bearing lemma, or repeated stalled milestone, plus a maximum interval. | Keep manual audits available and periodic independent full reviews. Unchanged files alone cannot prove that review has no value. |

## Validation and next experiment

The offline analyzer tests cover duplicate snapshots, independent author/child resets, fields disappearing and returning within or across counter epochs, absent cache/total fields, the reasoning/output relationship, deduplicated command identities, malformed/truncated JSONL tails, audit request/usage joins with numeric evidence validation, and ambiguous interleaved CLI sessions. Provider cache semantics remain distinct. These tests are part of the repository's normal `unittest` discovery. Scanning all four real research transcripts found no malformed records; rerunning after the partial-field and audit-linkage fixes left all documented historical totals unchanged.

No new paid proof run was launched as part of these efficiency measurements. A defensible next experiment runs baseline and revised workflows on the same held-out tasks with the same model/effort and comparable spend budgets, assesses completed arguments independently, and reports partial lemma quality as well as acceptance. The directed min-cut reference PDF must remain outside the solver's working context for any claimed autonomous rediscovery. Report observed cached/uncached/output tokens, missing-usage coverage, wall time, and independently checked success together. Until then the branch implements mechanisms motivated by observed overhead; it does not establish a cost-reduction percentage or guarantee equal proof success.
