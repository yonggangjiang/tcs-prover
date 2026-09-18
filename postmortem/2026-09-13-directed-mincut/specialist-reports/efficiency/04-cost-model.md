## Cost model of run 2026-09-13_17-06-14 (directed min-cut), scripts under /private/tmp/claude-503/-Users-yonggangjiang-Downloads-tcs-prover/62d437f4-1343-4f50-b460-a8439c134571/scratchpad/eff/

Scripts (all read-only over the transcript; one streaming pass in cost_model.py):
- cost_model.py -> calls.csv (per model call), injections.csv (every commandExecution/webSearch/subagent-result/user-message injection with bytes, tokens=bytes/3.6, re-send count), compactions.csv, windows.csv (context trajectory per thread between compactions), hours.csv, tasks.csv/tasks.json (125 subagent task segments); stdout in cost_model.out (per-thread totals, class aggregates, top-30 injections).
- classify_tasks.py -> tasks_classified.csv, classify.out (keyword classification of the 125 subagent results + per-task call cost).
- scenarios.py -> scenarios.out (pricing model, S1-S10, bundles, direct attributable-cost table). It contains a context-trajectory simulator (sawtooth with a compaction ceiling) so that scenario interactions are modelled instead of added.
- fruitless.py -> fruitless.out (attribution of calls to the activities the post-mortem judged fruitless).
- tables.py -> tables.out (per-hour bill shares, windows, low-output calls, hidden compaction cost).

### 1. Measurement corrections (measured)
- The transcript has 2,770 thread/tokenUsage/updated events, but 512 are re-emissions with identical `last` and identical cumulative `total` (333 in ROOT, mostly while it waited on subagents), and 30 are zero-usage markers emitted at each contextCompaction (e.g. transcript line 23616 at 15:46:19, `last.inputTokens=0`, total unchanged). Real model calls = 2,228. Summing `last` over all 2,770 events overstates input by 25% (335.8M vs 267.9M). Summing unique events, or the deltas of `total` across the two thread/resume resets (17:49:31, 20:13), gives exactly the context's totals: input 267,945,496; cached 258,896,896 (96.6%); uncached 9,048,600; output 1,830,446 incl. reasoning 910,754.
- Per thread (calls / input / mean context): ROOT 656 / 79.1M / 120.5k; literature_status 356 / 47.0M / 132.1k; structural_approaches 297 / 35.0M / 117.8k; batch_supersets 265 / 31.0M / 117.0k; flow_reduction 285 / 30.9M / 108.6k; audit_cleanup 207 / 26.5M / 127.9k; approximation_proofs 162 / 18.4M / 113.7k. Subagent threads = 70% of input tokens.
- Tier: job-settings.json `speedMode: "fast"`; the thread/start result reports `serviceTier: "priority"` (transcript line 4), Codex's name for fast mode; the runner only passes `features.fast_mode` for the goal thread (workflow_runner.py:1340) and `service_tier="fast"` for exec calls (workflow_runner.py:211-212).
- Compaction summaries are not in the recorded usage: the zero marker carries no tokens and the first call after a compaction has cached = 12,672-15,360 (stable prefix) and uncached 9k-28k (the summary). Summaries grew from 11.8k tokens (15:46) to 28k (19:38, 21:59).

### 2. Pricing model (assumptions stated)
Bill = tier x (U + r*C + 8*O), with U = uncached input, C = cached input, O = output (reasoning already inside O, OpenAI usage semantics), r = cached price ratio, output = 8x uncached. Tier 1.5x (runner docstring workflow_runner.py:200) or 2x; the multiplier cancels in every fraction except S1.
- r = 0.1: bill = 49.6M uncached-equivalents; shares uncached 18.2%, cached 52.2%, output 29.5%.
- r = 0.25: bill = 88.4M; shares 10.2% / 73.2% / 16.6%.
So 52-73% of the bill is the re-sent context; 17-30% is output (half of it reasoning).

### 3. Where the context comes from (measured bytes; tokens = bytes/3.6 assumed)
Within-window context growth sums to 6.32M tokens across 2,191 consecutive-call pairs; injected tool text is 11.5 MB = 3.13M tokens (49% of growth); model output 1.78M tokens (28%, reasoning appears to be replayed: uncached_(i+1) - growth_i has median 180 tokens); the rest is tool-call framing and root->subagent messages that are not items.

Direct attributable cost per class (first send uncached + (re-sends-1) cached; share of recorded bill at r=0.1 / 0.25):
| class | n | MB | cached re-sends (M tok) | share@0.1 | share@0.25 |
|---|---|---|---|---|---|
| APPROACHES node cats | 343 | 4.52 | 42.9 | 11.2% | 13.5% |
| AUDITS/*.md cats | 64 | 1.25 | 15.3 | 3.8% | 4.7% |
| PROVED.md slices | 51 | 0.72 | 9.4 | 2.3% | 2.9% |
| web (209 searches + 19 python page fetches) | 228 | 1.00 | 7.9 | 2.1% | 2.5% |
| PROVED+node multi-cats | 46 | 0.69 | 8.0 | 2.0% | 2.5% |
| python heredoc outputs (non-web) | 340 | 0.90 | 7.2 | 1.9% | 2.3% |
| INITIAL_PROMPT.md alone | 84 | 0.62 | 6.8 | 1.7% | 2.1% |
| INITIAL_PROMPT+node / +PROVED / +PROVED+node | 54 | 0.99 | 8.9 | 2.3% | 2.8% |
| index.md (+ mixed) | 23 | 0.27 | 3.9 | 0.9% | 1.2% |
| subagent results into ROOT | 125 | 0.15 | 0.9 | 0.2% | 0.3% |
| all injected text | 1,439 | 11.5 | 115.1 | 29.5% | 36.1% |
Cached re-sends of injected text = 44.5% of all cached tokens. Classification rule: a command is classed by the set of files it names; multi-file cats are shown separately (my INITIAL_PROMPT count is 84 pure + 71 mixed commands; the context's 203 also counts python heredocs that open it).

Top of the 30 costliest single injections (tokens x re-sends, full list in cost_model.out): 21:34:07 audit_cleanup `cat AUDITS/...` 59.5 KB x 71 re-sends = 1.17M cached tokens; 19:42:22 ROOT `cat AUDITS/*.md` 55.1 KB x 65 = 0.99M; 19:09:14 ROOT same 0.95M; 21:01:27 batch_supersets `cat APPROACHES/A001-verified-benchmarks.md` 71.2 KB x 47 = 0.93M; 17:57:01 ROOT AUDITS 0.92M; 20:14:47 audit_cleanup `cat A044` 65.8 KB x 50 = 0.91M; 21:16:57 ROOT AUDITS 0.90M; 20:41:46 ROOT AUDITS 0.89M; 20:58:54 batch_supersets AUDITS 0.89M; 20:14:44 approximation_proofs `cat A001*` 0.89M; 20:53:15 audit_cleanup rg over PROVED+AUDITS 50.9 KB 0.88M; 18:58:36 literature_status `sed -n 2309,2668p PROVED.md` 46 KB 0.87M. Six of the top ten are whole-audit-set cats (each ~1M cached tokens ~ 0.2% of the bill); the rest are whole-node cats.

### 4. Windows, compactions, the sawtooth, hidden cost (measured + one estimate)
- 30 compactions: 9 ROOT at 195k-226k (15:46, 16:32, 17:56, 18:31, 19:08, 19:42, 20:41, 21:16, 21:45), 21 subagent at 183k-240k. Windows: ROOT w0 n=57 19k->203k mean 119k; ... w9 n=53 33k->229k mean 139k; literature_status w1 24k->229k mean 153k; batch_supersets w1 32k->240k. Mean of all window means = 117k; a sawtooth from base 30k to ceiling 208k averages 119k. Consequence (the central finding): with a persistent thread under auto-compaction, cost per call = (base + ceiling)/2 regardless of what is read; read discipline alone only delays compactions. The levers with >5% effect are the ceiling, the thread lifetime, the number of calls, and the tier.
- Recovery ritual (commands within 180 s after a compaction, same thread): ROOT 441k tokens over 9 compactions = 49k per compaction (INITIAL_PROMPT 7.5 KB, index.md 14-23 KB, `cat AUDITS/*.md` 55-60 KB, PROVED head 25-45 KB, the working node 20-40 KB, e.g. 19:42:19-19:43:16); subagents 394k over 21 = 18.8k each. With ~60 re-sends each, the rituals account for ~9-10% of the bill.
- Hidden compaction cost (estimate, not in tokenUsage): 30 summaries = 556k output tokens plus 6.3M tokens of context read at compaction; if billed as ordinary output/cached input this is +10.8% (r=0.1) / +7.1% (r=0.25) on top of the recorded bill.
- Low-output calls: 743 of 2,228 calls (33%) produced <150 output tokens (one-line tool calls, `wait`, polling) yet each re-sent a ~120k context: 22.5% of the bill (r=0.1); in ROOT 203 of 656 calls = 5.4%. Median output per call 255 tokens, p90 2,171.

### 5. Per hour (UTC; share of recorded bill r=0.1 / 0.25)
15:00 237 calls 22.7M in 10.2%/9.4% | 16:00 373 calls 47.1M 17.1%/17.4% | 17:00 79 calls 9.2M 3.8% (paused 17:00-17:49 for audits) | 18:00 337 42.0M 15.7% | 19:00 357 42.7M 16.0% | 20:00 315 35.9M 13.0% | 21:00 464 60.2M 21.3%/21.8% | 22:00 66 8.0M 2.8%. Active time ~4.8 h -> ~460 calls/h across threads, ~137/h in ROOT.

### 6. Scenarios (bill fraction; simulator baseline reproduces the recorded bill to +7% and 37 vs 30 compactions, so fractions are scenario/simulated-baseline; second pair adds the hidden compaction cost)
| scenario | mechanism / formula | r=0.1 | r=0.25 | incl. hidden |
|---|---|---|---|---|
| S1 standard tier | bill/1.5 (bill/2 if fast is 2x) | 0.667 (0.500) | same | same |
| S2 read cap 4 KB/file (node, PROVED, AUDITS) | growth reduced by 5.9 MB; compactions 37->34; sawtooth keeps mean context | 0.992 | 0.997 | 0.984/0.991 |
| S3 one 3 KB overview replaces INITIAL_PROMPT reads + ritual | -49k tokens per ROOT compaction, -18.8k per subagent compaction, -1.6 MB prompt reads | 0.929 | 0.921 | 0.913/0.911 |
| S4 4 KB audit digest | AUDITS cats 1.25 MB -> 0.26 MB | 0.999 | 1.001 | 0.996/0.999 |
| S5 ceiling 160k | mean context 108k, 57 compactions, ritual unchanged | 0.922 | 0.878 | 0.983/0.919 |
| S5 ceiling 120k | mean 90k, 102 compactions | 0.881 | 0.785 | 1.061/0.904 |
| S5 ceiling 150k (branch default) | mean 103k, 64 compactions | 0.906 | 0.849 | 0.986/0.903 |
| S5+S3 160k / 150k / 120k | cheap re-anchor makes the lower ceiling pay | 0.839 / 0.822 / 0.765 | 0.792 / 0.766 / 0.681 | 0.872/0.816; 0.868/0.798; 0.867/0.749 |
| S6 task-scoped subagents | context reset to 12.7k prefix + 2k brief at each of the 125 task boundaries, same 1,572 subagent calls; subagent mean context 34-53k (not 30k: 12.6 calls per task accumulate reads) | 0.748 | 0.662 | 0.701/0.637 |
| S7 cap concurrency at 2, same calls | tokens unchanged; wall-clock of subagent work ~x3 | 1.000 | 1.000 | - |
| S8 web -70% bytes | 1.0 MB -> 0.3 MB | 0.999 | 1.003 | 0.997/1.001 |
| S9 critic/writer at 'high' (out of sample: 2 critic + writer + compile-repair calls, ~30k in + ~40k out each at ultra, output halved at high) | | -1.3% | -0.7% | - |
| S10 replayReasoning-style omission | not available: no replay/reasoning-item/encrypted option anywhere in workflow_runner.py; only model_reasoning_summary and the compaction settings are configured | 0 | 0 | - |
| B1 = S2+S3+S4+S8 | compactions 37->28 | 0.919 | 0.916 | 0.902/0.905 |
| B2 = B1+S5(160k) | | 0.824 | 0.783 | 0.843/0.796 |
| B3 = B1+S6 | 8 compactions | 0.686 | 0.596 | 0.632/0.567 |
| B4 = B1+S6+S5(160k); B4' at 150k | ROOT mean context 97k, subagents 31-47k | 0.659 / 0.650 | 0.557 / 0.544 | 0.618/0.537; 0.618/0.530 |
| B4+S1 realistic bundle | | 0.439 (0.330 at 2x) | 0.372 (0.279) | - |
Read the table as: token-side measures save 34-45% and the tier another third of what remains; S2/S4/S8 are near zero on their own because of the sawtooth, and become worth ~8% only via S3 and the shorter windows; a ceiling of 120k without a cheap re-anchor is a net loss once summaries are counted.

### 7. Activities the post-mortem judged fruitless (share of recorded bill; call->activity by the command the call produced, carried forward at most 6 calls; task segments from tasks.json)
- Reformatting after prompt edits: 16:02-16:12 (root notices "prompt layout changes" at 16:02:09 and dispatches "Revise only the Context and objective openings", results 16:06:26-16:07:49) 67 calls = 2.8%; 16:46-16:56 ("Migrated A011-A020", "Completed and released A021-A030", results 16:49:23-16:56:18) 79 calls = 3.7%; the 7 subagent tasks classed 'formatting' = 4.0%.
- index.md re-syncs (calls producing a command that touches index.md): 62 calls = 2.3%.
- Verification-class subagent tasks (keyword classifier: 46 tasks / 476 calls; the post-mortem's own count is ~50): 21.9% (r=0.1) / 22.7% (r=0.25). Verification+formatting+writing (70 tasks) = 38.3%.
- Certificate chain A035-A045 and lemmas L059/L060/L062/L065-L070: by command topic 401 calls = 18.4%; by subagent tasks whose result names those IDs 259 calls = 12.8%.
- Union without double counting: 998 calls = 46.0% (r=0.1) / 47.1% (r=0.25); 59% of the 16:00 hour and 76% of the 19:00 hour.
Subagent cost by task class: ROOT 27.0%, verification 21.9%, new-math 23.7%, writing 12.3%, other 6.0%, formatting 4.0%, literature 2.4%.

### 8. Status on branch fable-suggestion (checked, not run)
Already present: DEFAULT_COMPACTION_TOKENS=150000 and DEFAULT_CHECKPOINT_TOKENS=120000 passed as model_auto_compact_token_limit (workflow_runner.py:54-56, 1133-1134, 1339); token meter with TOKEN_REPORT_SECONDS (1153-1173); subagent re-anchoring (1381-1399); tools/records.py with overview/node/lemma/audits; CONTEXT DISCIPLINE, audit-digest.md and web rules (workflows/author_critic.yaml:108-114, 173); SUBAGENT POLICY one-task/2 KB brief/verify-once/at most 2 (yaml:116-120). The proposals below say what the model adds to these.

---

## Proposals

- **[E1] Default to the standard service tier** (risk none; saving: 33% of the bill (50% if fast is 2x); assumption: fast = 1.5x per the runner docstring, no change in tokens.)
  - mechanism: The whole bill is multiplied by the tier; the run used fast mode (serviceTier 'priority'). Time was declared less important than cost.
  - implementation: workflow_runner.py:37 SPEEDS/DEFAULT_SPEED 'fast' -> 'standard'; speed_arguments (workflow_runner.py:206-213) and the goal-thread features.fast_mode (workflow_runner.py:1340); job-settings speedMode default in the UI.
  - evidence: job-settings.json speedMode 'fast'; transcript line 4 thread/start result serviceTier 'priority'; workflow_runner.py:200 docstring 'OpenAI's 1.5x Fast mode'.
- **[E2] Enforce task-scoped subagents in the runner, not only in the prompt** (risk low; saving: 25% (r=0.1) to 34% (r=0.25) of the bill alone; 31-40% with the read discipline (B3). Assumptions: same 1,572 subagent calls, 12.7k cached prefix + 2k uncached brief per task, reads within a task unchanged.)
  - mechanism: Six persistent subagent threads averaged 109-132k context per call and were 70% of input tokens; resetting the context at each of the 125 task boundaries drops their mean context to 34-53k with the same calls.
  - implementation: SUBAGENT POLICY already in workflows/author_critic.yaml:116-120; add enforcement in the subagent branch of the event loop (workflow_runner.py:1381-1399): keep per-agent call and context counters from thread/tokenUsage/updated (dedupe re-emissions by comparing `total`), and when an agent exceeds ~25 calls or 60k context after its last 'completed' item, steer the root to close it and spawn a fresh one; write per-thread context to token-usage.json.
  - evidence: cost_model.out per-thread table; scenarios.out S6/B3 rows; tasks.csv 125 task segments (mean 12.6 calls each).
- **[E3] Keep the 150k ceiling only together with a cheap re-anchor (one overview call)** (risk low; saving: S5+S3 at 150k: 18% (r=0.1) / 23% (r=0.25); 13-20% once hidden compaction cost is counted. S5 alone at 150k: 9-15%, and at 120k a net loss (1.06) with hidden cost. Assumptions: summary billed as output, compaction read 95% cached, ritual = reads within 180 s after compaction.)
  - mechanism: Mean context per call is (base+ceiling)/2 under auto-compaction (measured 117k vs 119k predicted); lowering the ceiling cuts it, but each compaction adds a hidden 12-28k-token summary, a full-context read and a 49k-token ROOT recovery ritual. Only with the ritual replaced by a ~3 KB overview does the lower ceiling pay.
  - implementation: Already on the branch: DEFAULT_COMPACTION_TOKENS=150000 / DEFAULT_CHECKPOINT_TOKENS=120000 (workflow_runner.py:54-55, 1133-1134, 1339) and 'exactly one overview call' (workflows/author_critic.yaml:111); make the compaction steer (render('compaction'), workflow_runner.py ~1303-1311) itself carry the overview text so the model need not run any command, and do not go below ~140k.
  - evidence: compactions.csv (ROOT 195k-226k before, 24k-33k after); tables.out CONTEXT WINDOWS and HIDDEN COMPACTION COST (10.8% / 7.1%); scenarios.out S5 and S5+S3 rows; ritual 441k tokens over 9 ROOT compactions.
- **[E4] Cut round trips: batch shell reads/edits, stop idle polling of subagents** (risk none; saving: 8-11% of the bill if half of these calls are merged into their neighbours (they cost 22.5% at r=0.1); ROOT polling alone is 5.4%. Assumption: merged calls add no new tokens.)
  - mechanism: 743 of 2,228 calls (33%) produced fewer than 150 output tokens (bare ls/pwd/rg --files, single-file cats, wait/poll) yet each re-sent the full ~120k context; 203 such calls in ROOT alone.
  - implementation: Add to CONTEXT DISCIPLINE (workflows/author_critic.yaml:108-114): 'combine every independent read or edit of one step into a single command; never run a bare listing'; let tools/records.py accept several IDs per call; in the root's subagent handling prefer doing record work over `wait` (the 21 collabAgentToolCall 'wait' items and 512 re-emitted usage events show idle root turns).
  - evidence: tables.out LOW-OUTPUT CALLS; transcript 15:16:05-15:16:14 sequence of one-line commands each followed by a tokenUsage event at 93k-95k context.
- **[E5] Verify each lemma once, by a fresh subagent, at promotion** (risk medium; saving: ~10% of the bill assuming the number of verification tasks halves and each runs in a fresh 30-50k context (E2); up to 22% if verification were dropped, which is not recommended.)
  - mechanism: 46 subagent tasks (keyword classifier; post-mortem says ~50) were verification of sibling records and cost 21.9% of the bill; the branch's policy already says verify once.
  - implementation: workflows/author_critic.yaml:118 (policy exists); enforce by having the runner tag verification subagent briefs (brief text contains 'verify'/'audit') and refuse a second verification of a lemma ID already marked verified in PROVED.md; keep the critic stage as the safety net.
  - evidence: fruitless.out verification-class rows (476 calls, 61.3M cached tokens); classify.out task list.
- **[E6] Keep the read discipline (records.py, 4 KB audit digest, saved web pages) but do not count it as a saving by itself** (risk none; saving: 1-2% alone (S2 0.992/0.997, S4 0.999, S8 0.999); 8% as part of B1 with S3; it is what makes E2/E3's contexts stay small. Assumption: 4096 bytes per file named in a command, reads not repeated.)
  - mechanism: Capping node/PROVED/AUDITS reads at 4 KB removes 5.9 MB of injected text, yet under a fixed ceiling it only delays compactions (37->34 in simulation), so the bill barely moves; its value is fewer compactions (28 with S3), a cheaper ritual, less hidden summary cost and less state loss. Same for the audit digest (AUDITS cats were 1.25 MB, six of the ten costliest injections) and web pages (1.0 MB).
  - implementation: Already on the branch: tools/records.py, CONTEXT DISCIPLINE rules (workflows/author_critic.yaml:110-114), audit-digest.md (yaml:112,173, tests/test_audit_digest.py). Add a size guard in records.py (refuse --section all above 25 KB without --lines) and make the runner's steer after audits point at audit-digest.md only.
  - evidence: cost_model.out top-30 injections; scenarios.out S2/S4/S8/B1 rows; INJECTION COST TABLE (all injected text = 29.5-36.1% of the bill attributable, 44.5% of cached tokens).
- **[E7] Meter and cap the hidden compaction cost** (risk none; saving: 0 directly; it changes the optimum ceiling (see E3) and prevents a lower ceiling from silently costing more. Assumption: provider bills the summary as output.)
  - mechanism: Compaction calls are absent from tokenUsage: 30 summaries of 12-28k tokens (556k total) plus 6.3M tokens of context read; if billed as normal output/cached input this is +10.8% (r=0.1) / +7.1% (r=0.25) on the recorded bill, and it scales with the number of compactions (102 at a 120k ceiling).
  - implementation: In the token meter (workflow_runner.py:1153-1173) count contextCompaction items per thread and add an estimate (post-compaction first-call uncached tokens as summary size, context before as read); report it in token-usage.json and the status line; calibrate once against the provider dashboard.
  - evidence: transcript lines 23616-23629 (zero marker, then 24,470 input with 15,360 cached); compactions.csv; tables.out HIDDEN COMPACTION COST.
- **[E8] Deduplicate usage events in every tool that sums `last`** (risk none; saving: 0 (measurement); prevents budget alarms and post-mortems from mis-stating cost and calls per hour.)
  - mechanism: 512 of 2,770 thread/tokenUsage/updated events are re-emissions (identical last and total, mostly ROOT while waiting); summing `last` overstates input by 25% (335.8M vs 267.9M) and inflates the call count to 2,770 from 2,228.
  - implementation: workflow_runner.py:1153 already uses `total`; ui/audits.py and any transcript analysis should dedupe by (threadId, total.inputTokens) or use total deltas across thread/resume resets (17:49:31, 20:13:28).
  - evidence: cost_model.py dedupe logic; check in the analysis: sum of unique last == sum of total deltas == 267,945,496.
- **[E9] Lower reasoning effort for writer and compile-repair only (out of sample)** (risk low; saving: 0.7-1.3% of a bill of this size (assumptions: 30k input + 40k output per call at ultra, 20k at high); out of sample.)
  - mechanism: Critic/writer/reviewer did not run in this transcript; a typical tail is 2 critic + 1 writer + 1 compile-repair structured calls. Halving their output at 'high' effort saves a small fixed amount; keep the critic at ultra because it is the only correctness gate.
  - implementation: writer_effort/critic_effort options in audit_candidate/run_goal (workflow_runner.py:2411-2430, 2578-2600); set writerEffort 'high' in job settings, leave criticEffort 'ultra'.
  - evidence: job-settings.json criticRounds 2, writerEffort/criticEffort 'ultra'; workflows/clean_up.yaml latex_editor/latex_repair nodes; scenarios.out S9 line.
- **[E10] Concurrency cap of 2 is a time/quality choice, not a token saving** (risk medium; saving: 0% at equal work; per-hour spend falls by up to 2/3 of the subagent share if tasks are serialized, which delays the same work rather than removing it.)
  - mechanism: With the same calls, capping concurrent subagents at 2 leaves tokens unchanged and triples the wall-clock of subagent work; it saves tokens only by cutting the number of subagent tasks, which is a work reduction. The rate-limit death at 5.8 h is a per-hour quota problem that E1/E2/E3 address by lowering tokens per hour (from ~47M input/h at 21:00).
  - implementation: workflows/author_critic.yaml:120 'At most 2 subagents run at a time' is already there; pair it with the per-hour token alarm in the runner (TOKEN_REPORT_SECONDS meter, workflow_runner.py:1173) instead of relying on the cap for cost.
  - evidence: tables.out PER-HOUR (21:00 hour = 464 calls, 60.2M input, 21.3% of the bill); scenarios.out S7 line.
