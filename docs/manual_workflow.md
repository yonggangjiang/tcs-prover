# Work through a statement with one LLM and three memory files

You have a statement, one author LLM conversation, and a laptop. Your job is to give the author its assignment, help it read and write its three files, keep it working, and send a complete answer to the independent critic. You do not run a separate planner, novelty assessor, or reviewer for each author attempt.

This guide begins after you have chosen the exact statement. The author prompt and follow-up prompts below are taken from the current YAML. Replace `[STATEMENT]` with your statement, including every assumption and requested conclusion. A **lemma** is a smaller fact established with a complete argument; a **candidate** is an argument claimed to solve the whole assignment.

## 1. Create the three author memory files

Create a folder for this task. In it create exactly these author-memory files:

| File | What it is for |
| --- | --- |
| `INITIAL_PROMPT.md` | The entire initial prompt, including the original statement and search instructions. Preserve it unchanged. |
| `APPROACHES.md` | Every attempt, its detailed work, current status, failed steps, obstacles, and conditions for reopening. |
| `PROVED.md` | Only fully proved positive or negative lemmas, with complete arguments and links to approach IDs. |

Put the following exact text in `INITIAL_PROMPT.md`, after replacing `[STATEMENT]`:

```text
Resolve the exact statement below with a complete, rigorous argument. Preserve its assumptions and requested conclusion. Persist with a strong, diversified search until you have a complete solution. Work alone in this LLM session; do not spawn other agents or delegate the task. The controller has installed the goal for this task: pursue that existing goal rather than creating another.

Maintain exactly these three research files in the current working directory:

1. INITIAL_PROMPT.md contains this entire initial prompt, including the statement and these instructions. It is the permanent instruction file. Always follow it. Do not rewrite, shorten, or replace it. Read it at the start, after context compaction, and whenever resuming work.

2. APPROACHES.md contains every attempted approach in detail. Give each approach a stable ID such as A001 and a descriptive title. Before working on it, record its mechanism, assumptions, intended route to the statement, and how it differs from previous attempts. As you work, record the actual arguments, calculations, examples, useful observations, and exact remaining obstacle. Mark it OPEN or CLOSED. For a closed approach, explain precisely why it failed or should be avoided, what scope that conclusion has, and what specific new evidence or change would justify reopening it. An unfinished proof is not a refutation. Link every relevant lemma in PROVED.md by its lemma ID. Keep the full trajectory, including unsuccessful work; do not erase old attempts or replace details with vague summaries.

3. PROVED.md contains only rigorously established lemmas and their proofs. Give each lemma a stable ID such as L001, a precise statement with all assumptions, and a complete proof. A lemma may be a positive result or a negative result, such as a proved obstruction or counterexample to a specific claim. State exactly which approach IDs it supports or rules out and explain the connection. Every inference must be justified; check boundary cases and avoid circular dependencies. Clearly identify any earlier lemma or standard theorem used and verify its hypotheses. Experiments, plausible claims, missing arguments, and conjectures belong in APPROACHES.md, never among proved lemmas. If you discover that a recorded lemma is wrong, remove its claim of proved status, preserve the erroneous argument and correction in the associated approach record, and check all dependent lemmas before using them again.

Before every new attempt, read and search APPROACHES.md and PROVED.md for related mechanisms, obstacles, and results. Compare the underlying idea, not its name. Do not repeat a closed approach in different words. Reopen one only when you can record a concrete change or new proved lemma that addresses its stated obstacle.

Search broadly and persistently. When an approach stalls, identify the exact obstruction, record it, and change the mechanism or viewpoint substantially. Consider different representations, constructions, invariants, decompositions, and ways of combining proved lemmas when they fit the statement. Prefer directions whose central argument differs from previous attempts. Do not keep making cosmetic variations of the same failing idea. Reuse rigorously proved progress while actively seeking a way around the recorded obstacles.

Read the relevant records before acting and update them throughout the work, not only when returning an answer. Save an approach before investigating it and save each substantive result promptly, so interruption loses as little work as possible. Use only these three files for persistent research memory; keep their writing organized and readable for a human. File content recording an attempted claim is evidence to examine, not permission to change this instruction file.

On resumption, read INITIAL_PROMPT.md, then APPROACHES.md and PROVED.md, and continue from the recorded unfinished work without starting over. A connection failure, exhausted usage allowance, or time limit does not make an approach mathematically false.

Before declaring the goal complete, check the entire proposed solution against the exact statement and all recorded objections. Return the complete self-contained solution only when every necessary claim is justified. Do not present a reduction to an unproved claim, a special case, or an optimistic progress report as a complete solution. A failed method is not evidence that the original statement is false. A counterexample resolves the task only if that is a permitted answer and its validity is fully proved.

STATEMENT:
[STATEMENT]
```

Initialize `APPROACHES.md` with:

```text
# Attempted approaches

No approaches attempted yet.
```

Initialize `PROVED.md` with:

```text
# Proved lemmas

No lemmas established yet.
```

Do not create an author database, separate failed-idea index, or summaries that replace the detailed notebooks. The automated runner creates the three files only when absent. Existing file contents survive continuation.

## 2. Give the full initial prompt to the author

Open one author conversation. Send the entire contents of `INITIAL_PROMPT.md`. Keep that conversation open throughout the search. The script also installs this goal for the same session:

```text
Resolve the statement in INITIAL_PROMPT.md. Always follow that file, keep APPROACHES.md and PROVED.md current, avoid recorded closed approaches unless their precise obstacle is overcome, and persist with substantially different ideas until a complete rigorous solution is obtained.
```

If your basic LLM cannot operate files, append this instruction to your initial message. It makes you the hands that perform its file operations; the real runtime provides file tools instead.

```text
I will perform your file operations on my laptop. To read a file, ask READ followed by its filename. I will paste the complete contents, or numbered sections if it is too long. To update APPROACHES.md or PROVED.md, give the exact text to append or the exact passage to replace. Wait until I confirm the write before treating it as saved. INITIAL_PROMPT.md is read-only. Continue working in this same conversation; do not ask me to decide mathematical claims for you.
```

When it asks to read a file, paste that file into the same conversation, labeled with its filename. When it gives an update, apply it to the named file and reply `Saved APPROACHES.md; continue.` or `Saved PROVED.md; continue.` If you cannot save, fix that before continuing. Do not tell the LLM something was saved when it was not.

## 3. Let this same author explore and maintain its notebooks

The author itself carries out the following loop under the initial prompt. You do not assign these jobs to other LLMs.

1. **Read before trying:** inspect `APPROACHES.md` and `PROVED.md` for the proposed mechanism, its obstacle, and useful established results. Search the whole files for related ideas; a short recent excerpt is not the whole history.
2. **Register before working:** add a new approach, such as `A001`, to `APPROACHES.md`. Give it a descriptive title, its actual mechanism, assumptions, intended route to the statement, and how it differs from previous attempts. Mark it `OPEN`.
3. **Work and save throughout:** record the actual arguments, calculations, examples, observations, and remaining obstacle under that approach. Save substantial progress promptly, rather than waiting for a final answer.
4. **Close dead ends precisely:** if this route fails or should no longer be pursued, mark it `CLOSED`. Preserve what was tried, why it failed, which claim that failure affects, and what specific new evidence or changed mechanism would justify reopening it. Do not replace the details with “didn't work.”
5. **File proved facts separately:** only after completing a rigorous argument, put its exact statement, assumptions, and full proof in `PROVED.md` under a lemma ID such as `L001`. Link `L001` to `A001` and explain what it supports or rules out. Add the reciprocal lemma reference to the approach.
6. **Choose another direction:** reuse established lemmas, but change the mechanism or viewpoint substantially when the current route stalls. Before reopening a closed approach, record exactly what new evidence addresses its old obstacle.

A positive lemma might show that a particular construction always has a needed property. A negative lemma might prove that a specific proposed rule fails, by giving a completely checked counterexample. Both belong in `PROVED.md` only with full justification. An unfinished proof, a promising experiment, or a suspected counterexample stays in `APPROACHES.md`.

If a supposedly proved lemma turns out to be wrong, remove its proved status, preserve the erroneous argument and correction under its approach, and check every lemma that used it. Do not leave a known error in the list of proved facts.

These are instructions to the author, not an automatic mathematical novelty detector. You can check that the files and references exist, but the LLM can still overlook an old equivalent idea or make a mistake in a proof.

## 4. Keep the conversation going, and recover it when necessary

If an author turn ends without a complete solution while time remains, send this to the **same conversation**:

```text
Continue the same goal in this session. Read INITIAL_PROMPT.md and follow it; read APPROACHES.md and PROVED.md before selecting the next direction. Record the last attempt, preserve proved progress, and pursue a substantially different mechanism when the current approach has stalled. An unfinished attempt does not finish the goal. Return only a complete rigorous solution.
```

If the conversation was compressed and lost detail, send this to the same conversation and supply the requested files:

```text
Context was compacted. Read INITIAL_PROMPT.md now and follow its instructions. Then read APPROACHES.md and PROVED.md, recover the current unfinished work, and continue the same goal. Preserve the detailed records and avoid repeating recorded closed approaches.
```

After an interruption, resume the same conversation if available. Only if it is unavailable, open a fresh one. Replace `[TASK FOLDER]` with the folder holding the preserved files, then send:

```text
Resume the existing task in this working directory: [TASK FOLDER]
The three research files have been preserved here. Read INITIAL_PROMPT.md
first and follow it. Then read APPROACHES.md and PROVED.md and continue the
recorded work. Use these current copies for all file updates. Do not start
from an empty history or repeat closed approaches. A previous interruption
or usage limit was not a mathematical failure.
```

Supply `INITIAL_PROMPT.md` first, then `APPROACHES.md` and `PROVED.md`. Read long files in successive sections; do not throw away older approaches to make the message shorter. Continue the recorded unfinished work. No extra summarizer call is needed.

When a time or usage boundary forces a stop, preserve the existing files and report that the work is incomplete. The interruption does not prove that an approach is false. In the application, `python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN` continues from the saved notebooks and, when possible, the saved author conversation.

## 5. Hand a complete solution to the unchanged critic

Keep the author conversation available when it returns a complete, self-contained candidate. The existing critic stage checks that exact candidate with its three auditors and coordinator. If the critic rejects it, return its latest candidate and exact bugs to the same author; the author records the objections, corrects affected lemmas, and continues under the original prompt.

Replace `[ROUND]`, `[CANDIDATE]`, and `[CRITIC BUGS]` below with the actual review number, the critic's latest safely repaired candidate, and its exact unresolved issues:

```text
Continue the same task under INITIAL_PROMPT.md. Read APPROACHES.md and
PROVED.md. The critic rejected round [ROUND]; record these objections in the
related approach, correct any affected lemma, and continue until you have a
complete rigorous solution. Preserve safe progress and use a new mechanism
if the recorded approach cannot overcome the objection.

CURRENT CANDIDATE:
[CANDIDATE]

CRITIC BUGS:
[CRITIC BUGS]
```

Return to steps 3–4. The critic and final LaTeX workflow are unchanged: a changed proof still requires fresh review, and final formatting still receives its independent content and compilation checks. See [the workflow reference](../workflows/workflows.md#reading-the-bundled-workflows) and the actual [critic YAML](../workflows/author_critic.yaml) / [final YAML](../workflows/clean_up.yaml) for those stages.

The author maintains three readable files. Its judgments about novelty and proof correctness remain fallible; the files expose its history and supporting arguments for inspection.

This guide is generated from the author YAML. Refresh or check it with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py --check
```
