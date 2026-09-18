# Prompt-level cost comparison: xean vs tcs-prover (run 2026-09-13_17-06-14)

Scope: COST only. Measured facts come from `transcript.jsonl` (scripts in `/private/tmp/claude-503/.../scratchpad/eff/{tok,ritual,reads,sub,spawn,resid}.py`) and the pre-extracted views; estimates are labelled and their assumptions stated. Times are UTC, 2026-09-13. One accounting note: summing per-call `tokenUsage.last` gives 335.7M input tokens and the final per-thread `tokenUsage.total` counters sum to 172M, versus the 269.8M in the task brief; I use the brief's totals for shares and my per-call sums only for ratios (average context, cache fraction, per-thread proportions), which do not depend on the counting method.

Cost model used for every estimate (assumption, not measured): price(cached input) = 0.1 x price(input), price(output) = 8 x price(input), the usual GPT-5-class ratios. Then the run costs 9.05 (uncached) + 25.9 (258.9M cached x 0.1) + 14.4 (1.8M output x 8) = 49.3M input-equivalents: cached reads 52%, uncached 18%, output 29%. With a 25% cache discount cached reads would be 71%. Either way the dominant term is (number of model calls) x (context per call), and every lever below acts on one or both factors.

## 1. What each system puts in front of the model per call, and how much is stable

| | xean (one role call) | tcs-prover author / subagent (one Codex turn step) |
|---|---|---|
| Stable, cache-keyed part | The role `system` string only: explorer ~500 words (pi-roles.ts:194-208), coordinator (246-258), verifiers (288-300). Cache key = sha256(role label + system) (pi-roles.ts:599-601), so every call of a role shares one prefix. `docs/application-author.md:86`: "Use `system` for stable role definitions and contracts"; the task, changing guidance and corrections go in `prompt`. | Codex base instructions + tool schemas: **12,672 tokens**, measured as the cached count on the first call after every subagent compaction (e.g. 16:04:09, 16:14:59, 16:20:22 ... 22:05:53). ROOT's prefix is 15,360 (15:46:19), consistent with the 7,481-byte INITIAL_PROMPT sitting in the initial user message. This prefix alone is 12.7k x 2,770 calls = 35M cached tokens, 13% of all cached input. |
| Ordering for cache hits | User prompt is task text first (158-159), then the notes JSON, which is append-only across turns (summaries, support, verdicts), then support texts, then guidance (209-224). Verifier calls that judge the same notes share task + support + notes and differ only in the trailing verifier name and obligation (331-334, 315-329). Output schema kept identical across candidates so the provider schema is stable (roles.ts:550-551). | Codex thread order: prefix, initial user message, then everything since the last compaction in arrival order. 98.2-99.5% cache hit (control_events 15:16:01, 15:33:30, 16:11:54) because each call resends the same growing body; the cost is paid on the body's size, not on misses. |
| What is deliberately omitted | PASS reports of every verdict (`promptNote`, pi-roles.ts:161-175: "Successful explanations are not working memory"); `verification.report` (only the source name is kept); the text of every note that is not coordinator-selected support (roles.ts:202-207 `notes: noteFields.omit({text:true})`); support proofs for the source verifier (546: "Established support proofs have already been checked by correctness and are omitted"); the note's own text for the reconstruction proof call (399); earlier reasoning items when `replayReasoning:false` (74-76, 590; README.md:78). | Nothing is omitted by construction. The model chooses what to read with a shell: 1,092 commands injected 10.5 MB. Residency simulation (4 bytes/token): injected file text is **46% of ROOT's average 126k context** (58k tokens), 19-54% in the subagents, ~39% overall. |
| Who decides the packet | The runtime; the model cannot read beyond it. Verification packets are capped at `window` = 100,000 chars of note + support text (pi-roles.ts:95-97,106; workflow.ts:166-196). | The model. The prompt instructs re-reading: "Read it [INITIAL_PROMPT.md] at the start, after context compaction, and whenever resuming work" (author_critic.yaml:91), and "On resumption, read the current reports" (:130). Result: `cat INITIAL_PROMPT.md` 126 full + 19 partial reads (1.92 MB), AUDITS (~60 KB set) read 47x by ROOT (724 KB) and 43x by subagents; node files 425 reads / 4.63 MB; PROVED.md 175 reads / 1.60 MB (143 full `cat` of node files vs 165 ranged reads). |
| Verification reuse | One verdict per (note, verifier) per candidate; a call that already has a verdict is not rerun and an interrupted verification resumes at its missing checks (727-734); identical requests are settled from the journal (872-897, 1093-1105); source PASS passages are reused so pages are not reopened (947-1009); a note with no external premise gets a source PASS without any model call (1011-1033). | No notion of "verified once": 97 of 125 subagent results were verification or formatting; cross-verification re-reads proofs; 123 of ROOT's 172 python commands were link/DAG/hash self-checks of the records. |
| Continuation text | "Keep trying, you can do it." (pi-roles.ts:234) or the default two-sentence feedback (src/pi.ts:883-889), plus a finalize message at the context threshold. It can be tiny because the entire state is in the packet. | `continuation` never delivered (workflow_runner.py:1348-1349 fires only when blocked/complete). Compaction steer (author_critic.yaml:166-169) = "Read INITIAL_PROMPT.md now ... Recover the relevant saved context". Measured ritual per ROOT compaction: 14-27 commands, 109-329 KB (15:46:19, 16:32:03, 17:56:51, 18:31:12, 19:08:51, 19:42:12, 20:41:38, 21:16:41, 21:45:32). Over all 30 compactions: 431 commands (39% of all shell commands) and 4.70 MB (45% of all injected bytes) inside 12-minute windows. |
| Web | Explorer and verdict verifiers: "Do not use web search or external tools" (202, 298). Only the Codex source verifier searches, capped at `maxSourceWebActions` = 16 (109), killed by the runtime at the cap (source.ts:259-300, 713-722), told "Use a short targeted search ... return INCONCLUSIVE ... instead of repeating searches" (549). | 209 searches across 7 threads (ROOT 28, literature_status 62, batch_supersets 51), 141 distinct; 21 searches of arxiv 2407.10830v1; 19 shell fetches (urllib/curl) printed into context, largest 61 KB (audit_cleanup). Web residency is small (2-5% of context) but the searches are calls at ~120k context each. |
| Process hygiene for Codex | Source verifier: fresh CODEX_HOME, minimal env (source.ts:466-505), `--ephemeral --ignore-user-config --ignore-rules --sandbox read-only`, `skills.include_instructions=false`, `include_environment_context=false`, `include_permissions_instructions=false`, `include_apps_instructions=false`, `include_collaboration_mode_instructions=false`, `project_doc_max_bytes=0`, `tools.update_plan.enabled=false` (641-689), 21 features disabled (339-361). | Author: default Codex app-server prompt plus `web_search=live`, `service_tier=fast`, `model_auto_compact_token_limit_scope=body_after_prefix` (workflow_runner.py:212, 217-221). Auditors: fresh CLI, read-only tools (ui/audits.py:148-175). |
| Audit / review | review.ts: one Codex call, prompt = JSON `{task, argument}` (67-79), completed submissions reused (98-121). | research_audit.yaml:27-42 gives no packet and no reading order ("the author has saved their work in ... APPROACHES/**/*.md ... and PROVED.md"; goal 2 demands per-file readability review). Auditors crawled 53 (claude-fable, 96 Read events) and 46 (kimi-k3, 65) distinct files of a 2.4 MB workspace; author paused 17:00:06-17:49:23 and 19:49:23-~20:14 = 74 min (21% of the run). Auditor tokens unrecorded; estimate >=0.6M uncached input per auditor per batch (2.4 MB / 4). |

Subagent structure is the largest single difference: xean has no persistent helper threads; every role call is a fresh `runPi` (SPEC.md:88). tcs-prover ran six persistent Codex subagents at 108-133k average context for 8-35 turns each; they consumed 190M of 269.8M input tokens (70%), exchanged 881 messages with ROOT (409 of ROOT's 998 calls, 41%, were subagent-message events), and produced 125 results totalling 134 KB.

## 2. xean prompt-level rules that would cut tcs-prover's per-call context

1. **Abstract-first records (summary vs text).** xean shows every note's summary and only selected texts (roles.ts:202-207; pi-roles.ts:250); the coordinator writes the summary once, "never copies proof text" (248). tcs-prover analog: a <=40-word ABSTRACT at the top of every node and lemma, read with `rg`, bodies opened only by line range. Targets the 46% resident-file share.
2. **PASS prose is not working memory** (161-175). Analog: a verified lemma carries one `Verified:` header line; verifier text never enters node files (which reached 1.4 MB, A001/A055 >80 KB each). Failures keep full reports.
3. **Verify once, journal the verdict** (727-734, 872-897, 1093-1105). Analog: never re-verify a lemma whose text is unchanged; never verify tools, obstruction lemmas or CLOSED nodes. Directly removes most of the 97 verification/formatting subagent results.
4. **Reuse inspected passages; conclude without a call when possible** (947-1033). Analog: a looked-up primitive becomes a STANDARD entry in PROVED.md with URL and section, so it is never searched again (21 searches on one URL in this run).
5. **Hard web cap + INCONCLUSIVE** (109, 549, source.ts:293-296). Analog: per-turn search budget, one fetch per source per run, never print a page; stop with INCONCLUSIVE.
6. **Fresh-context roles with a self-contained packet** (SPEC.md:88; review.ts:67-79). Analog: one-task subagents spawned with the statement and the exact text to judge, no workspace crawl, no chat; and an audit packet instead of a workspace crawl.
7. **Stable-before-changing ordering and one stable system string** (158-159, 331-334, 599-601). Analog: keep INITIAL_PROMPT in the prefix and never re-inject it (the `resume` prompt currently re-embeds `{original_prompt}`, author_critic.yaml:170-179, a second 7.5 KB copy); strip the Codex preamble parts the author never uses (as source.ts:673-685 does).
8. **Tiny continuation because state is in the packet** (234; pi.ts:883-889). Analog: a compaction/continuation prompt that points at one <=60-line PLAN block instead of "recover the relevant saved context", replacing the 14-27-command ritual.
9. **replayReasoning:false** (README.md:78). Not available as a Codex CLI setting that I could verify in this repo; listed as an experiment, not a proposal: 0.91M reasoning tokens were generated and, if replayed until compaction, could occupy a substantial share of the unexplained 54% of ROOT's context.

## 3. Proposed concrete text for tcs-prover

Insert blocks A-C into `prompts.author` in `workflows/author_critic.yaml` after the file-structure section (line 130) and before "Return a complete, self-contained solution" (line 132); they are saved into INITIAL_PROMPT.md verbatim like the rest. Block D replaces `compaction` (166-169), `continuation` (161-165) and the `{original_prompt}` part of `resume` (170-179). Block D assumes the PLAN block of postmortem section 6.1 exists at the top of `APPROACHES/index.md` with a `## Nodes` heading before the table, and adds one PLAN line.

### A. Reading discipline (author prompt)

```
READING DISCIPLINE (cost rules; they never override the statement)
R1. Your initial message already contains this entire prompt and the STATEMENT. Never run
    `cat INITIAL_PROMPT.md`. If after a compaction you cannot see the STATEMENT, run
    `sed -n '/^STATEMENT:/,$p' INITIAL_PROMPT.md` once; no other read of that file is permitted.
R2. Every node file and every PROVED.md lemma starts with `ABSTRACT:` (at most 40 words: what it
    establishes or why it failed, and its status). Read abstracts before bodies:
    `rg -n '^# A|^ABSTRACT' APPROACHES/*.md` and `rg -n '^## L|^Statement' PROVED.md`.
    Open a body only by line range (`sed -n 'a,bp'`) after locating the section with
    `rg -n '^#' <file>`. Never `cat` a node file or PROVED.md whole.
R3. Open a proof only when its exact hypotheses are load-bearing for the step you are writing
    now. Statements are cheap; proofs are not. Never re-read a lemma you already cited or
    verified in this context.
R4. Read AUDITS/ once per batch, then write a <= 10-line AUDIT RESPONSES entry in the PLAN.
    Do not reopen the reports; the PLAN entry is your record of them.
R5. Do not re-read a file you just wrote. Do not run link, DAG or hash validation scripts unless
    you added or renamed a node this turn, and then run one script once.
R6. Reading budget: at most 40 KB of file text per turn and one read of at most 8 KB during a
    compaction recovery. If you need more, you are reading bodies instead of abstracts; write
    the missing abstract instead.
R7. Verification prose is not working memory. When a lemma or node passes verification, record
    one header line `Verified: <who>, <UTC time>, <scope>` and keep the verifier's text out of
    the records. A FAIL or INCONCLUSIVE keeps its full report in the node's Obstacles section.
```

### B. Subagent-usage policy (author prompt; `multi_agent` stays enabled)

```
SUBAGENT POLICY
S1. A subagent is a fresh reader. Its spawn message is a self-contained packet: the STATEMENT;
    the task in at most 10 lines; the exact text it must judge or use, pasted (lemma statements,
    the section under review); the files it may open (at most 3, with line ranges); and the
    answer format. Never write "read the workspace". A subagent does not read INITIAL_PROMPT.md,
    index.md, AUDITS/ or any file outside its list.
S2. One task per spawn. The subagent ends with its result and is never reused. Do not keep
    long-lived subagents, do not exchange messages with a running subagent, and do not `wait`
    in a loop; send a new packet instead.
S3. Allowed tasks. SOLVE: attack one precisely stated MISSING step from the STATEMENT and a
    one-page brief (PLAN plus the statements, not proofs, of the lemmas on the BEST ROUTE), with
    no access to APPROACHES/ or PROVED.md bodies until it has written its own candidate.
    VERIFY: one lemma, statement and proof pasted; verdict PASS / FAIL / INCONCLUSIVE with the
    concrete defect; no web. LOOKUP: one primitive; return its exact statement, hypotheses,
    running time and citation. Not allowed: formatting, migration, link checking, index
    maintenance, restating records; do those yourself with one script.
S4. Result contract: first line `RESULT: <verdict or one-sentence outcome>`, then at most
    300 words. You file it: a VERIFY pass becomes one `Verified:` line (R7); a SOLVE result
    becomes a node section you write; a LOOKUP result becomes a STANDARD entry in PROVED.md
    with the citation, so it is never looked up again.
S5. Budget: at most 2 subagents alive at once; each at most 40 model responses.
S6. Verify once. A lemma with a `Verified:` line is not sent again unless its text changed.
    Never verify tools, obstructions against your own proposals, or CLOSED nodes.
```

### C. Web-fetch policy (author prompt; subagents inherit it through S1)

```
WEB AND FETCH POLICY
W1. Search only for (a) the exact statement and cost of a black-box primitive you intend to
    cite, or (b) an item of the integralization checklist. Statements that a problem is open
    or hard are not evidence: do not search for them, do not record them, and do not let one
    change the PLAN.
W2. Budget: at most 3 searches per turn and one fetch per source per run. Before searching,
    check PROVED.md for a STANDARD entry; if the primitive is recorded, use it.
W3. Never print a fetched page into the context. Save it outside the records
    (e.g. /tmp/src.html), extract with `rg -n -C3 '<theorem number or name>'`, print at most
    60 lines, and record the theorem verbatim as a STANDARD entry in PROVED.md with URL and
    section.
W4. If the exact statement is not found within budget, write `INCONCLUSIVE: <what is missing>`
    in the node and move on; do not retry with query variants.
W5. A VERIFY subagent may not search at all.
```

### D. Compaction / resume recovery with one small read (lifecycle prompts)

Add to the PLAN spec one line: `RESUME: node <ID>, section "<heading>", next action: <one sentence>, lemma statements needed: <IDs>` (rewritten at the end of every turn and when the runner's pre-compaction steer fires).

```
compaction: >-
  Context was compacted. Your initial message still holds the STATEMENT and the instructions;
  do not re-read INITIAL_PROMPT.md. Make exactly one read:
  `sed -n '1,/^## Nodes/p' APPROACHES/index.md` (the PLAN block, at most 60 lines). Its RESUME
  line names the current node, the section you were writing and the next action. Continue from
  that action. Do not list files, do not validate links or the DAG, do not open AUDITS/, and do
  not open any node or lemma until the next action requires a specific line range of it.

continuation (deliver at every turn boundary; runner fix from postmortem 6.4.1): >-
  Same statement. Before opening any file, rewrite the RESUME line and, if the MISSING step
  changed, the PLAN. Then execute the PLAN's next action. End the turn by pasting the PLAN only.

resume (replace the ORIGINAL INITIAL AUTHOR PROMPT section): >-
  Resume the task in this working directory. The instructions and STATEMENT are in
  INITIAL_PROMPT.md; the thread already contains them, so read only
  `sed -n '1,/^## Nodes/p' APPROACHES/index.md`. If AUDITS/ has new reports, read each once and
  answer them in the PLAN's AUDIT RESPONSES; then follow the RESUME line.
```

Cost of block D per compaction: one command of <= 6 KB, against the measured 109-329 KB and 14-27 commands per ROOT compaction.

## 4. xean ideas that would weaken tcs-prover if copied naively

1. **Explorer handoff every 4 responses / fresh explorer each turn** (`maxExplorerResponses` 4, pi-roles.ts:108; new explorer sees only summaries + selected support, 250). tcs-prover's best mathematics came from open-ended persistent threads in the first 40 minutes (postmortem P3) and unrecorded root reasoning was already lost at compaction (P2). Forcing a handoff every 4 responses turns every idea into a note before it is ripe and routes all continuity through summaries, which the run shows are what the author remembers wrongly (L019 remembered as "non-algorithmic"). Keep the persistent author; cap the body, checkpoint the RESUME line, and use fresh contexts only for SOLVE/VERIFY/LOOKUP subagents.
2. **"Do not use web search" for the explorer** (202). This problem requires exact black-box primitives (almost-linear max-flow, decomposition theorems); a total ban weakens the author. Cap and purpose-type searches (block C) instead.
3. **Summaries only, texts on request through a coordinator** (roles.ts:202-207). Hiding lemma statements behind a summary lets the author build on hypotheses it misremembers; the postmortem's C2/C5 are exactly that failure. Statements with exact hypotheses must stay cheap and visible (R2/R3 keep statements, hide proofs).
4. **A separate coordinator call per turn with "no correctness authority"** (255). It adds one full-packet call per turn and moves planning out of the author; the postmortem's PLAN block keeps planning in the author with no extra call. Cost-neutral at best, and it splits the plan from the mathematician.
5. **Reconstruction verification (3 calls per note, 359-362) for every lemma.** xean applies it only to notes claiming completion (251). Applied to 73 lemmas it would cost ~220 fresh calls of full-proof packets; reserve it for the final candidate and lemmas on the BEST ROUTE.
6. **A content-free continuation** ("Keep trying, you can do it.", 234). It works in xean because the packet is the state. In tcs-prover the state lives in files; a content-free reopen is what the Codex goal loop already did (postmortem P5) and it produced record maintenance instead of planning. Block D's continuation is small but points at the PLAN.
7. **Dropping all verdict prose.** Correct for PASS (R7), wrong for FAIL/INCONCLUSIVE, which xean also keeps (161-175); obstruction reasons are what stop the author from re-entering a dead branch.
8. **Stripping the Codex preamble from the author.** Safe for the source verifier (no shell), but the author needs shell and file tools; only the optional includes listed in proposal P7 can be removed.

## Combined estimate

Not additive because proposals overlap (reads, subagents and compaction rituals are the same tokens counted from different angles). Adopting P1-P5 and P7 together: calls fall from 2,770 to roughly 1,500 (fewer reads, no subagent chat, no rituals) and average context from ~123k to ~60-70k, giving an input-token product roughly 30-35% of the measured one; output tokens fall less (fewer but denser turns). Rough combined effect: 55-65% of run cost removed, wall-clock 20% shorter from not pausing for audits. The main risk is in P2 (subagent continuity) and P5 (audit depth); both are mitigated by keeping the SOLVE role open-ended and letting auditors open files on demand after a Phase-0 plan.

---

## Proposals

- **[P1] Reading discipline: abstract-first, ranged reads, no INITIAL_PROMPT/AUDITS re-reads, verify-once header lines** (risk low; saving: 15-25% of run cost. Assumptions: injected bytes fall from 10.5 MB to ~4.3 MB (INITIAL_PROMPT 1.92 MB -> 0; AUDITS 1.42 MB -> 0.1 MB; PROVED 1.60 -> 0.4 MB via statement-only reads; nodes 4.63 -> 2.0 MB via abstracts and ranges); resident share of context falls from ~39% to ~16% (average context -23%); ~500 of 2,770 calls disappear (-18%); cached input (52% of cost at a 10% cache price) falls by (1-0.23)(1-0.18) ~ 37%.)
  - mechanism: Cuts the resident file text that is re-sent on every call (46% of ROOT's average 126k context, ~39% overall) and removes the read commands themselves (each read is a model call at ~120k context). ABSTRACT lines and `rg` outlines replace whole-file `cat`; PASS prose stays out of records so files stop growing; INITIAL_PROMPT.md (already in the cached prefix) and AUDITS/ are never re-read.
  - implementation: workflows/author_critic.yaml `prompts.author`: insert block A (READING DISCIPLINE R1-R7) after line 130 (AUDITS paragraph) and before line 132; delete the clause 'Read it at the start, after context compaction, and whenever resuming work' at line 91 and replace with 'It is in your initial message; do not re-read it'; add 'ABSTRACT:' as the first line of the node template (lines 106-112) and of each PROVED.md lemma (line 126). Same text lands in INITIAL_PROMPT.md automatically.
  - evidence: reads.py: 126 full `cat INITIAL_PROMPT.md` + 19 partial; 143 full-cat node reads vs 165 ranged; AUDITS read 47x by ROOT (724 KB) and 43x by subagents; resid.py: resident file-read tokens 58k of 126k (ROOT), 19-54% in subagents; author_critic.yaml:91 and :130 instruct the re-reads; postmortem P1 (1.4 MB nodes, 900 KB PROVED.md); xean pi-roles.ts:161-175 (PASS reports omitted), roles.ts:202-207 (summaries, texts only for support).
- **[P2] Subagent policy: one-task fresh spawns with a self-contained packet, no chat, verify once** (risk medium; saving: 35-45% of run cost. Assumptions: subagent input falls from 190M to ~75M tokens (one-shot spawns average ~35k context and ~40 calls; 97 of 125 tasks either vanish (formatting, re-verification) or shrink); ROOT loses ~280 subagent-message calls of 998 (-28% of ROOT input); output tokens roughly unchanged. Both figures are extrapolations from the measured per-thread mix, not a measured pilot.)
  - mechanism: Replaces six persistent 108-133k-context helper threads (70% of input tokens) and 881 ROOT<->subagent message exchanges (409 of ROOT's 998 calls) with short fresh spawns (SOLVE / VERIFY / LOOKUP) that receive the statement and the exact text to judge, may open at most 3 files, and return <=300 words once. Verified lemmas get a header line and are never re-verified, removing most of the 97 verification/formatting results.
  - implementation: workflows/author_critic.yaml `prompts.author`: insert block B (SUBAGENT POLICY S1-S6) after block A. Keep `features: [multi_agent]` (line 9). Runner (workflow_runner.py ~1280-1281, postmortem 6.4.4): send the compaction steer to subagent threads or, better, enforce S5 by cancelling a subagent thread after 40 responses. The SOLVE role is the fresh-eyes solver of postmortem 6.5; S3 keeps it open-ended so early-run mathematics is not lost.
  - evidence: Per-thread input (task brief): subagents 190.3M of 269.8M; sub.py: ROOT item mix 375 commandExecution, 409 subAgentActivity, 28 webSearch; spawn.py: 881 'interacted' events, subagent turns 8-35 each; subagents.txt: 125 results totalling 134 KB (avg ~1 KB); postmortem P3 (97 of 125 results verification/formatting, none given only the statement); xean SPEC.md:88 (every role call is a fresh interaction), review.ts:67-79 (packet = {task, argument}).
- **[P3] Web-fetch policy: purpose-typed searches, per-turn cap, one fetch per source, never print a page, INCONCLUSIVE at cap** (risk none; saving: 2-4% of run cost (209 searches ~ 7.5% of calls; web residency 2-5% of context; 19 shell fetches up to 61 KB). The value is mostly in strength (postmortem C3) rather than tokens.)
  - mechanism: Each search or fetch is a model call at ~120k context and the fetched page stays in context until compaction. A cap plus a STANDARD-entry rule (record the theorem once in PROVED.md) removes repeated lookups; the extraction rule keeps pages out of the context; the 'open-problem statements are not evidence' rule removes the searches that anchored the run.
  - implementation: workflows/author_critic.yaml `prompts.author`: insert block C (WEB AND FETCH POLICY W1-W5) after block B. Optionally runner: count webSearch items per turn from item/completed and steer once the budget is hit (mirrors xean's source.ts:293-296 hard stop).
  - evidence: sub.py: 209 searches, 141 distinct, 21 on https://arxiv.org/html/2407.10830v1, 19 urllib/curl fetches, largest 61,305 bytes (audit_cleanup); resid.py web residency 0-6k tokens per call; xean pi-roles.ts:109 (maxSourceWebActions 16), :549 (short targeted search, INCONCLUSIVE instead of repeating), :202/:298 (no web for explorer/verifiers).
- **[P4] Compaction/resume recovery limited to one <=6 KB read of the PLAN block (with a RESUME line)** (risk low; saving: 10-15% of run cost on its own (overlaps with P1; P1+P4 together ~25-30%). Assumptions: the 431 ritual commands and 4.70 MB (45% of injected bytes) shrink to 30 commands and ~0.2 MB; ritual bytes are also the text that then sits in context for the rest of each window.)
  - mechanism: Replaces 'Read INITIAL_PROMPT.md now ... Recover the relevant saved context' with one `sed -n '1,/^## Nodes/p' APPROACHES/index.md`. The RESUME line (node, section, next action, lemma IDs needed) is rewritten at every turn end and at the runner's pre-compaction steer, so recovery needs no directory listing, no index validation, no AUDITS re-read and no node re-read. Smaller post-compaction context also delays the next compaction.
  - implementation: workflows/author_critic.yaml: replace `compaction` (lines 166-169), `continuation` (161-165) and the ORIGINAL INITIAL AUTHOR PROMPT part of `resume` (170-179) with block D; add the RESUME line to the PLAN spec (postmortem 6.1) and require a `## Nodes` heading before the index table. Runner: deliver `continuation` at every turn boundary (workflow_runner.py:1348-1349) and steer 'rewrite the RESUME line now' at ~140k tokens (postmortem 6.4.2) so the line is fresh when compaction fires.
  - evidence: ritual.py: per ROOT compaction 14-27 commands and 109-329 KB (15:46:19, 16:32:03, 17:56:51, 18:31:12, 19:08:51, 19:42:12, 20:41:38, 21:16:41, 21:45:32); 30 compactions total 431 commands / 4.70 MB in 12-minute windows; first post-compaction call cached 12,672 (subagents) / 15,360 (ROOT) tokens, so the prefix and initial message survive compaction; spawn.py: 123 of 172 ROOT python commands were structural self-checks; xean pi-roles.ts:234 and src/pi.ts:883-889 (tiny continuation because state is in the packet).
- **[P5] Audit packet instead of workspace crawl; no author pause** (risk medium; saving: 6-10% of total spend and ~20% of wall-clock. Assumptions: each auditor read ~2 MB (>=0.5M uncached input tokens) per batch; 3 auditors x 2 batches ~ 3-6M uncached tokens ~ 3-6 of the 49.3M input-equivalents; packet audits cost ~30% of that. Author pause measured at 74 min of 5.8 h. Auditor tokens are not recorded, so this is the least certain estimate.)
  - mechanism: Auditors receive a controller-built packet (PLAN, index table, ABSTRACT lines of ACTIVE nodes, statements of lemmas on the BEST ROUTE; ~50-80 KB) and may open at most N files by line range after writing their Phase-0 plan (postmortem 6.3). Runs on a read-only snapshot without pausing the author. The author gets a one-page verdict tally, not 55-60 KB of reports.
  - implementation: workflows/research_audit.yaml `prompt` (lines 27-42): replace the workspace description with the packet and the phased structure of postmortem 6.3; drop goal 2's per-file readability audit or cap it at 15% of the report. ui/audits.py run_auditor (148-175): pass the packet as the prompt and keep read-only tools; controller merges reports into a one-page tally written to AUDITS/ so the author's R4 read is small.
  - evidence: control_events: claude-fable 96 Read events over 53 distinct files, kimi-k3 65 over 46; PROVED.md (904 KB) read 15 times across auditors; pauses 17:00:06-17:49:23 and 19:49:23-~20:14; AUDITS set 55-60 KB re-read 90 times by the author threads (1.42 MB); xean review.ts:67-79 (single packet), pi-roles.ts:95-97 window cap; postmortem P4 (audits restated the premise).
- **[P6] Record diet through verify-once and PASS-prose omission (xean promptNote analog)** (risk none; saving: 5-10% of run cost, mostly overlapping with P1 (smaller files -> smaller reads -> smaller resident context). Assumption: node volume grows at half the measured 1.4 MB / 5.8 h rate.)
  - mechanism: Stops node and lemma files from absorbing verifier prose and repeated cross-verification text, so every later read (P1) is smaller and compactions come later. Verified lemmas carry one header line; obstruction proofs against the author's own proposals go into the parent's Obstacles section, not PROVED.md.
  - implementation: Covered by R7 and S6 in blocks A/B plus postmortem 6.6 (opening <=12 lines, link lemma IDs instead of restating). Optional runner validator (postmortem 6.4.8): reject node writes >25 KB without an ABSTRACT line.
  - evidence: APPROACHES/ 1.5 MB (A001, A055 >80 KB), PROVED.md 904 KB; postmortem C4 ('each was cross-verified'), P1; xean pi-roles.ts:161-175 ('Successful explanations are not working memory'), :727-734 (verdict recorded once, never rerun).
- **[P7] Trim the Codex prefix and lower the compaction ceiling (runner flags, not prompt text)** (risk none; saving: 2-5% of run cost for the prefix trim (unmeasured: the removable share of the 12.7k prefix is unknown; 20-30% assumed) plus whatever a lower ceiling yields, which depends on P4 being in place (otherwise more compactions mean more rituals).)
  - mechanism: The cached prefix is 12,672 tokens on every one of 2,770 calls (35M cached tokens, 13% of cached input). xean's source verifier disables the optional Codex includes; the author could drop the same optional parts while keeping shell and file tools. A lower `model_auto_compact_token_limit` (or dropping `body_after_prefix`) keeps average context below the measured 108-133k.
  - implementation: workflow_runner.py context_cache_arguments (217-221) and the app-server thread config (~1239-1240): add `-c skills.include_instructions=false -c include_environment_context=false -c include_apps_instructions=false -c include_collaboration_mode_instructions=false -c project_doc_max_bytes=0 -c tools.update_plan.enabled=false` (as xean source.ts:673-685) after checking each is accepted by the app-server; set an explicit `model_auto_compact_token_limit` around 120-140k once P4 makes recovery cheap.
  - evidence: ritual.py: cached tokens on the first post-compaction call = 12,672 in every subagent thread; 2,770 calls; xean source.ts:641-689 flag list; workflow_runner.py:217-221 (`body_after_prefix` lets windows grow to 167-240k, tok.py max ctx 207-240k per thread).
- **[P8] Deliver a small state-pointing continuation at every turn boundary (xean-style, but pointing at the PLAN)** (risk none; saving: ~0% direct; enables P4 (the one-read recovery depends on a current RESUME line) and removes the turn-end progress-report messages (88 ROOT agentMessages, 27 KB).)
  - mechanism: Cost-neutral on tokens; it prevents the content-free goal-loop reopen that produced record maintenance and progress reports, and it keeps the RESUME line fresh so P4's one-read recovery works.
  - implementation: workflow_runner.py:1348-1349: call `start_turn(render('continuation'))` (or a steer) on every turn/completed while the goal is active; `continuation` text from block D.
  - evidence: postmortem P5 (zero deliveries); control_events shows only compaction steers and one live instruction (16:39:38) as user messages; sub.py: ROOT userMessage count 13; xean pi-roles.ts:234 / pi.ts:883-889.
