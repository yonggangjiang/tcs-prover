# Cost-efficiency proposal for tcs-prover (separate from the strengthening changes)

Scope: token and quota cost of the author harness, measured on the failed run `runs/2026-09-13_17-06-14_design-and-analyze-an-algorithm-for-exact-global/` and compared with the mechanisms of the `xean` toolkit (https://github.com/chaoxu/xean). The strengthening changes (`REPORT.md` §6) are taken as given; this document only says what makes the same research cheaper without weakening it. Method: five specialist analyses (xean mechanisms, xean prompt design, a transcript cost model with a scenario simulator, an activity-value classification, a harness settings review), one synthesis, three adversarial verifiers (numbers, strength, feasibility) and a revision. The verbatim revised proposal with its 18 load-bearing claims is `specialist-reports/efficiency/00-revised-proposal-verbatim.md`; the verifier objections are `01-verifier-objections.md`; the evidence reports are `02`–`06`. Everything below is checked against those files; the "Status" column says what is already on the `fable-suggestion` branch.

## 1. Where the run's cost went (measured, deduplicated)

Codex re-emits cumulative usage events, so naive sums overstate calls by 24%. Deduplicated:

| Quantity | Value |
|---|---|
| Real model calls | 2,228 (2,770 events) over 5.8 active hours |
| Input tokens | 267.9M, of which 258.9M cached (96.6%) and 9.0M uncached |
| Output tokens | 1.83M (0.91M reasoning); the root author produced 484k of it |
| Mean context per call | 120k (sawtooth between ~30k after a compaction and 183–240k at compaction) |
| Subagent threads | 6 persistent threads, 1,572 calls, 70.5% of input, 74% of output; 125 results at 12.6 calls and 1.5M input each |
| Inter-agent messaging | 889 calls (464 subagent→root reports, 425 delegations) = 41% of all tokens |
| Injected file text | 1,092 shell commands injected 10.6 MB; 44.5% of all cached tokens are re-sends of that text |
| Whole-file reads | INITIAL_PROMPT.md read by 203 commands (108 whole cats); node A001 (83 KB) read 41 times; audit reports read by 64–68 commands |
| Verification-class subagent work | 19.7–21.9% of the bill; 24–27 of 49 passes produced real corrections (missing hypotheses or cases), so it is not waste |
| Service tier | Everything ran on the fast/priority tier (`~/.codex/config.toml` also sets `service_tier = "priority"`) |
| Provider quota | The weekly Pro window went from 0% at 15:06 to 100% at 22:06 UTC; the run died inside a remote compaction |

Unit of account: the constraint that ended the run was the weekly quota, whose weighting of cached, uncached, output and tier is not identifiable from one run. Bill fractions below assume list-price ratios (cached 0.1 or 0.25 of uncached, output 8x) and are labelled estimates. The fast-tier multiplier (the runner's own docstring says 1.5x) is unmeasured.

Central measured fact: under a persistent thread with auto-compaction, cost per call is roughly (base + ceiling)/2 whatever is read. Read discipline alone therefore mostly delays compactions (simulated effect under 1%); the levers with a simulated effect above 5% are, in order, thread lifetime (context reset per task), the compaction ceiling paired with a cheap re-anchor, and the tier.

## 2. What xean does that transfers, and what does not

Adopted (with the adaptation):

| xean mechanism | Adaptation here |
|---|---|
| Fresh-context roles: a call gets the task, note metadata and selected support texts, never earlier transcripts | Single-task subagents with a ≤2 KB brief that end after one report; the author recovers from its records with one `records.py overview` call, not from re-read files |
| One structured submission per call, no narration; stable rules in instructions, changing directions as user messages | "Report exactly once"; the only prose of an author turn is the PLAN pasted at the end; strategy rules live in the prompt, the continuation/checkpoint messages carry only what changes |
| Bounded continuation (`maxExplorerResponses`, context budget with a reserve) | Explicit `model_auto_compact_token_limit` plus a PLAN checkpoint 30k tokens before it; root turn time cap; subagent call cap as a safety net |
| Verify once, record the verdict, never re-verify; PASS prose omitted, FAIL/INCONCLUSIVE kept | `**Verified.**` line per lemma, `overview` lists lemmas lacking it, one fresh verifier with a bounded `records.py brief` packet |
| Verification packet bounded by a character window | `brief L034` = the lemma in full plus the statements of the lemmas it cites, capped |
| Web-action cap observed by the runtime | Runner counts searches and fetches per rolling hour and steers the author at the budget; prompt caches paper excerpts in the node that uses them |
| Guidance frozen at turn boundaries | Audit digests and fresh-eyes reports enter through the continuation message, not mid-turn |
| Complete accounting; "efficiency work waits for measured spend" | Deduplicated cross-thread meter, quota events, `token-usage.json`, a measurement plan (section 5) |

Not copied, and why: explorer hand-off every four responses (the run's best mathematics came from open-ended threads early on; what compaction lost was unrecorded reasoning, which the PLAN checkpoint addresses); no web for the explorer (exact primitives are needed; cap and cache instead); summaries instead of statements (hides the hypotheses the author must build on; the post-mortem's C2/C5); a separate coordinator call per turn (the PLAN block gives the discipline at zero extra calls); reconstruction verification of every lemma (reserve for the final candidate); content-free continuation ("Keep trying" works when the packet is the state; here the state is in files, so the continuation must point at the PLAN); `replayReasoning: false` (no Codex option exists); the SQLite journal (the app-server owns thread state).

## 3. The efficiency changes, ranked, with status on the branch

Effects overlap and are not additive. "Simulated" refers to the cost model's scenario fractions of a simulated baseline that itself overshoots the recorded run by 8–16%, so treat them as bands.

| # | Change | Expected effect | Strength risk | Status |
|---|---|---|---|---|
| 1 | Task-scoped, single-report subagents: one task per spawn, ≤2 KB brief, no interim messages, no agent-to-agent chat, root does not poll; `agents.max_concurrent_threads_per_session=2`; call cap 40 as a safety net; subagents re-anchored after their own compactions | Context reset per task: simulated 0.66–0.75 of baseline; removing messaging calls is an upper bound of a further 23% of tokens (the thinking inside those calls migrates) | medium: the run's best early result followed several interim messages; SOLVE-type subagents may ask one clarifying question | Prompt + runner + config on branch; measure in pilot F2 |
| 2 | Standard service tier by default | 0–33% of the bill if the 1.5x docstring holds; 0 if the quota ignores tier | none | `DEFAULT_SPEED = "standard"` on branch. `codex app-server` has no `--ignore-user-config`, so a `service_tier` line in `~/.codex/config.toml` must be removed by hand (README says so) |
| 3 | Records-derived recovery with an explicit 150k compaction ceiling and a checkpoint at 120k: one `overview` call (7 KB on this run instead of 49k tokens of re-reads per root compaction), section reads, statements before proofs, `tool_output_token_limit=6000`, the statement carried inside the compaction message | Simulated 0.77–0.82 for the ceiling with a cheap re-anchor; the ceiling alone buys little and a 120k ceiling without a re-anchor is neutral or negative | medium: more compactions; mitigated by the PLAN checkpoint | On branch (`compaction_tokens`, `checkpoint_tokens`, `tool_output_tokens`, `records.py`, compaction prompt). The `body_after_prefix` scope override is removed so the explicit limit bounds the whole context |
| 4 | Verify once per promoted lemma, in a small context, with a bounded packet; never lower verifier effort by default | Verification is ~20% of the bill; a bounded pass per promoted lemma is the estimated replacement (unmeasured) | low–medium: verification found real gaps, so it stays at the author's effort | Prompt rules and `brief`/`Verified.` support on branch; runner refusal of a second VERIFY not implemented |
| 5 | No narration, no INITIAL_PROMPT.md re-reads, batched reads, deterministic validation (`records.py validate`), lean resume template (the old one re-pasted the 7 KB author prompt into the thread at every resume) | Narration 121 calls (5.5% of tokens, upper bound), INITIAL_PROMPT reads 97 calls (3.9%), ad-hoc validators 67 calls (3.3%), resume copies ~1.2M cached | none | On branch |
| 6 | Audit digest, per-batch time box, machine-readable verdicts; fresh-eyes solver on a brief | Audit reads were 3.8–4.7% static (≈0 simulated); the time box bounds the author's idle time (73 min in this run) | low | On branch. Auditing a snapshot without pausing the author is not done: it would change a documented contract and a passing test |
| 7 | Web budget in the runner (12 actions per hour, one steer per hour) plus cached paper excerpts | 2–2.5% static, ≈0 simulated; prevents the 25-fold refetching seen for one arXiv paper | low | On branch (prompt + runner) |
| 8 | Per-role effort: author, critic, verification and proof-writing subagents at ultra; lookup and formatting subagents at high; writer and reviewer could drop to xhigh | 1–3% (mostly output) | medium if misapplied | Prompt policy on branch; `subagent_effort` knob exists but inherits by default |
| 9 | Accounting: deduplicated cross-thread meter every 10 minutes, `token-usage.json`, quota status per decile from Codex `account/rateLimits/updated`, checkpoint request 5 points before the quota threshold, pause at 90% instead of dying at 100% | 0% direct; prevents the total-loss mode that ended this run | none | On branch |
| 10 | Prefix trim: `include_apps_instructions=false` on the author thread | ≈1–2% (a few thousand cached tokens on every call) | none | On branch; further Codex prefix flags (skills, environment context) are left for the pilot |
| 11 | Residuals: root turn time cap 45 min (checkpoint request), unsaved subagent results flushed on fatal errors, transcript delta suppression (disk only, not done), critic as checkpointed parallel audits (not done), LaTeX repair loop cap (not done) | 0–2% each | none | Partially |

## 4. Combined estimate and what not to book

The only joint figures are simulator bundles: read discipline + task-scoped subagents + a 150k root ceiling gives roughly 0.65–0.82 of the baseline bill at the same tier (0.54–0.77 under the alternative cached-price ratio), before any tier change and before the added cost of the strengthening changes (a fresh-eyes solver at ultra per audit batch, phased audits, checkpoint messages). Messaging removal could lower this further by at most 23% of tokens; expect less. The standard tier multiplies the result by 1/1.5 if the docstring holds and by 1 if the quota ignores the tier. In quota terms: a run that consumed the whole weekly window would plausibly consume half to two thirds of it at the same tier, and less on the standard tier, with the same author effort and the same verification rigor.

Not savings, or not worth their strength cost: cutting author effort below ultra (the author's reasoning is ≈5% of the bill and the failure was conceptual); killing subagents (the cost was their lifetime and inherited context, not their existence); disabling web search; shrinking the critic; the concurrency cap as a cost lever (same calls, same tokens); read caps, digests and web diets counted on their own (they pay only through the ceiling and the context reset); dropping or cheapening verification; a 120k ceiling without a cheap re-anchor; reasoning-replay suppression (no Codex option).

## 5. Measurement plan

Log per run (the meter and `token-usage.json` now provide the first two): per-thread deduplicated calls and token classes; per-subagent task calls, tokens and interactions; bytes injected per command class; searches and fetches per hour; every quota reading with its timestamp; wall time by state; counts of narration calls, INITIAL_PROMPT.md reads (target 0), whole-file cats (target 0), lemmas verified more than once (target 0).

Pilots, each two hours or less, before the next long run:

- F0 Launchability: start one live goal session and confirm `initialize`, the resolved `serviceTier`, that a `cat` of an 80 KB node is truncated by the tool-output cap, and that the compaction limit takes effect.
- F1 Tier A/B: 60 minutes standard versus 60 minutes fast on the same statement; compare quota percent per hour at matched tokens (this run: about 14–15 points per hour). Decides whether change 2 is worth 0%, 33% or 50%.
- F2 Subagent regime: 60 minutes with the new policy; pass if input per subagent result is at most 0.4M (was 1.5M), calls per result at most 8 (was 12.6), interactions per task at most one each way, verification still catches a seeded defect, and at least one exploration result of the substance of the run's early results.
- F3 Compaction: two 2-hour runs at 150k and 110k with the overview re-anchor; pass if uncached tokens in the ten calls after a compaction stay under 10k (was 23–113k) and compactions per hour stay at most 3; otherwise keep 150k.
- F4 Bundle: one 4-hour run with everything on; targets mean context at most 70k, injected bytes at most 4 MB per 5.8 hours, quota at most 50% per 5.8 hours at the same tier, and the strength criterion of `REPORT.md` §7 (ledger rows i–iii tried before any row-vi node).

## 6. Files changed on the `fable-suggestion` branch for efficiency

- `workflow_runner.py`: standard tier default; explicit compaction limit and checkpoint; tool-output cap; subagent concurrency and effort knobs; subagent call cap and re-anchoring; deduplicated cross-thread token meter with budgets; quota alarm and pre-pause checkpoint; web-action budget; app-instruction prefix trim; statement carried in lifecycle messages; unsaved-subagent flush.
- `workflows/author_critic.yaml`: context discipline, subagent policy, batching, narration limit, fetch caching, `**Verified.**` rule; lean compaction, continuation and resume messages.
- `workflows/research_audit.yaml`, `ui/audits.py`: time box, machine-readable verdicts, digest, fresh-eyes solver on a brief.
- `tools/records.py`: `overview`, `node`, `lemma`, `brief`, `grep`, `audits`, `validate`.
- Tests: `tests/test_context_economy.py`, `tests/test_records_tool.py`, `tests/test_audit_digest.py`.
