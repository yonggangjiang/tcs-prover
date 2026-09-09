#!/usr/bin/env python3
"""Generate the three-file author guide from the actual workflow YAML."""

import argparse
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workflow_runner import render_template


def block(prompt):
    return "\n```text\n" + prompt.rstrip() + "\n```\n"


def render():
    aw = yaml.safe_load((ROOT / "workflows/author_critic.yaml").read_text())
    author = aw["nodes"]["author"]
    ap = aw["prompts"]
    original = ap[author["prompt"]].replace(author["marker"], "[STATEMENT]", 1)
    bindings = author["lifecycle"]
    lifecycle = {key: key for key in bindings} if isinstance(bindings, list) else bindings
    parts = ["""# Work through a statement with one LLM and three memory files

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
"""]
    parts.append(block(original))
    for filename in ("APPROACHES.md", "PROVED.md"):
        parts.append(f"\nInitialize `{filename}` with:\n")
        parts.append(block(render_template(author["files"][filename], {"original_prompt": original, "statement": "[STATEMENT]"})))
    parts.append("""
Do not create an author database, separate failed-idea index, or summaries that replace the detailed notebooks. The automated runner creates the three files only when absent. Existing file contents survive continuation.

## 2. Give the full initial prompt to the author

Open one author conversation. Send the entire contents of `INITIAL_PROMPT.md`. Keep that conversation open throughout the search. The script also installs this goal for the same session:
""")
    parts.append(block(ap[lifecycle["goal"]]))
    parts.append("""
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
""")
    parts.append(block(ap[lifecycle["continuation"]]))
    parts.append("\nIf the conversation was compressed and lost detail, send this to the same conversation and supply the requested files:\n")
    parts.append(block(ap[lifecycle["compaction"]]))
    parts.append("\nAfter an interruption, resume the same conversation if available. Only if it is unavailable, open a fresh one. Replace `[TASK FOLDER]` with the folder holding the preserved files, then send:\n")
    parts.append(block(render_template(ap[lifecycle["resume"]], {"directory": "[TASK FOLDER]"})))
    parts.append("""
Supply `INITIAL_PROMPT.md` first, then `APPROACHES.md` and `PROVED.md`. Read long files in successive sections; do not throw away older approaches to make the message shorter. Continue the recorded unfinished work. No extra summarizer call is needed.

When a time or usage boundary forces a stop, preserve the existing files and report that the work is incomplete. The interruption does not prove that an approach is false. In the application, `python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN` continues from the saved notebooks and, when possible, the saved author conversation.

## 5. Hand a complete solution to the unchanged critic

Keep the author conversation available when it returns a complete, self-contained candidate. The existing critic stage checks that exact candidate with its three auditors and coordinator. If the critic rejects it, return its latest candidate and exact bugs to the same author; the author records the objections, corrects affected lemmas, and continues under the original prompt.

Replace `[ROUND]`, `[CANDIDATE]`, and `[CRITIC BUGS]` below with the actual review number, the critic's latest safely repaired candidate, and its exact unresolved issues:
""")
    parts.append(block(render_template(ap[lifecycle["repair"]], {
        "round": "[ROUND]", "solution": "[CANDIDATE]", "bugs": "[CRITIC BUGS]"})))
    parts.append("""
Return to steps 3–4. The critic and final LaTeX workflow are unchanged: a changed proof still requires fresh review, and final formatting still receives its independent content and compilation checks. See [the workflow reference](../workflows/workflows.md#reading-the-bundled-workflows) and the actual [critic YAML](../workflows/author_critic.yaml) / [final YAML](../workflows/clean_up.yaml) for those stages.

The author maintains three readable files. Its judgments about novelty and proof correctness remain fallible; the files expose its history and supporting arguments for inspection.

This guide is generated from the author YAML. Refresh or check it with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py --check
```
""")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    path = ROOT / "docs/manual_workflow.md"
    result = render()
    if arguments.check:
        if not path.is_file() or path.read_text() != result:
            raise SystemExit("Manual differs from YAML; run python3 docs/render_manual.py")
        print("Author guide matches current YAML prompts and file templates.")
    else:
        path.write_text(result)
        print(path)


if __name__ == "__main__":
    main()
