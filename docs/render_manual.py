#!/usr/bin/env python3
"""Render the operator manual with prompts/contracts taken from the actual YAML."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow_runner import _instructions, load_workflow, render_template


def describe_schema(schema, indent=""):
    lines = []
    for name, field in schema.get("properties", {}).items():
        kind = field.get("type")
        if "enum" in field:
            detail = "one of " + ", ".join(repr(value) for value in field["enum"])
        elif kind == "array":
            detail = "a list"
            if "minItems" in field:
                detail += f", at least {field['minItems']} items"
            if "maxItems" in field:
                detail += f", at most {field['maxItems']} items"
        else:
            detail = {"string": "text", "boolean": "true or false", "integer": "a whole number",
                      "object": "an object with the fields below"}.get(kind, kind)
        lines.append(f"{indent}{name}: {detail}")
        if kind == "object":
            lines.extend(describe_schema(field, indent + "  "))
        elif kind == "array":
            item = field["items"]
            if item.get("type") == "object":
                lines.append(indent + "  Each list item has exactly these fields:")
                lines.extend(describe_schema(item, indent + "    "))
            else:
                lines.append(indent + "  Each item is " + {"string": "text"}.get(item.get("type"), str(item)))
    return lines


def prompt_block(prompt, schema):
    form = "\n".join(describe_schema(schema))
    return ("Send this prompt:\n\n```text\n" + prompt.rstrip() + "\n```\n\n"
            "With a basic chat model, append this response instruction to the same message. "
            "The automated pipeline supplies the equivalent JSON schema directly.\n\n```text\n"
            "Return one JSON object. Include every field below and no additional fields. "
            "Preserve nested objects and lists. Use empty text or an empty list when the prompt "
            "says that field has no content.\n\n" + form + "\n```\n")


def render():
    author = load_workflow(ROOT / "workflows/author_critic.yaml")
    cleanup = load_workflow(ROOT / "workflows/clean_up.yaml")
    ap, cp = author["prompts"], cleanup["prompts"]
    research = author["nodes"]["author"]
    steps = research["research"]["steps"]
    critic = author["nodes"]["critic"]
    audit = critic["parallel"]
    values = {
        "assignment": ap[research["prompt"]].replace(research["marker"], "[STATEMENT]", 1),
        "statement": "[STATEMENT]", "memory": "[BRIEFING]", "feedback": "[LATEST CRITIC FEEDBACK]",
        "instruction": "[LATEST HUMAN INSTRUCTION]", "proposal": "[CURRENT PROPOSAL]",
        "result": "[CURRENT RESULT]", "related": "[RELATED RECORDS]",
    }
    parts = ["""# Operate the persistent workflow yourself, using a basic LLM and files

Start with a statement that you have already decided is the exact assignment. Your job is to give the LLM one assignment at a time, file its answer, and follow the next-step rules. You do not have to judge a mathematical argument yourself to operate this procedure: separate LLM calls provide the assessments, and you enforce the recording and routing rules.

The prompts below come directly from the current workflow YAML, with its instruction blocks already combined. Bracketed uppercase labels are the only things you replace. The numbered steps describe the default pipeline after statement approval; they do not require using the optional statement reviewer.

A **candidate** is an argument claimed to solve the whole task. A **mechanism** is the actual way an approach is supposed to work. A **family** groups approaches that use substantially the same idea. An **obligation** is a particular claim that still needs an argument. A **refutation** is checked evidence that a particular claim fails; running out of time is not a refutation.

## 1. Set up the permanent notebook before asking for ideas

Create one folder for this statement. Inside it create `research/records/` and these files:

| File | What you put there |
| --- | --- |
| `research/STATEMENT.md` | The exact original statement. Preserve it unchanged. |
| `research/INDEX.md` | A navigation list of record IDs, their kind, and links to their files. |
| `research/STATE.md` | The current round, next step, current proposal/result or their record IDs, latest feedback, recent assigned families, candidate status, and open obligations. Start at round 1, step “propose”, with the other lists empty. |
| `research/FAILED.md` | Links to failed, rejected, or unfinished records, preserving each record's status and reopening condition. |
| `research/PROVED.md` | Links to reviewed reusable results and candidates, showing assumptions and review status. The filename does not mean every listed claim is proved. |

Assign each new record the next ID, such as `e000001`. Save it as `research/records/e000001.md`. Each card contains its ID, date/time, kind, the complete prompt if it is a model call, the complete answer, and the IDs of relevant earlier records. Do not replace an old card with a correction: file a new card and link it to the earlier one. Save every explicit proposal, assessment, assignment, result, audit, rejection, repair, and final document.

The script keeps this permanent information in `research.sqlite3` and generates the Markdown notebooks. As a manual operator, your complete record cards are the filing cabinet. `STATE.md` and the indexes are your replaceable workbench. Do not delete old cards to shorten the workbench. The program's navigation indexes show recent records; older records remain saved and searchable.

**Before every model call:** file the exact prompt and set the next step to “awaiting this reply.” **After every reply:** file the full reply, then update the next step. If the laptop or model stops midway, record an interruption and resume that unfinished call. An interrupted request does not make the idea false. If you cannot save a result, fix storage before asking for more work.

Set a time or cost boundary you are willing to spend. The automated default allows 168 hours for the workflow and bounds each model request by the time remaining. A pause preserves the next step; it does not declare success.

## 2. Prepare the material that goes into each prompt

Open a fresh LLM conversation for each planning, assessment, exploration, review, audit, coordination, or editing call below. Persistence comes from the files you provide, so the next call does not depend on remembering an old conversation.

Replace these labels literally:

| Label in a prompt | What you paste |
| --- | --- |
| `[STATEMENT]` | The exact contents of `research/STATEMENT.md`. |
| `[BRIEFING]` | Recent research records with their IDs and status, plus the open obligations and recent assigned families. Initially say “No research yet. Open obligations: none. Recently assigned families: none.” |
| `[LATEST CRITIC FEEDBACK]` | The latest complete coordinating critic report. Initially use `{}`. |
| `[LATEST HUMAN INSTRUCTION]` | Your latest additional instruction, or leave it empty. |
| `[CURRENT PROPOSAL]` | One complete proposal from the planning answer. |
| `[RELATED RECORDS]` | Retrieved earlier cards relevant to that proposal, with IDs. Initially use `[]`. |
| `[CURRENT RESULT]` | The complete exploration result card. |
| `[CANDIDATE]` | The complete current candidate argument, without shortening it. |
| `[THREE AUDITS]` | The actual three completed auditor replies, in auditor order. |
| `[OPEN OBLIGATIONS]` | Every still-open issue with its ID and description. Initially use `[]`. |
| `[ACCEPTED SOLUTION]` | The exact argument that passed review without being changed in that review. |
| `[FORMATTED LATEX]` | The editor's entire returned document. |
| `[FINALIZATION FEEDBACK]` | The previous formatting-verifier and compiler reports when retrying finalization. Initially use `{}`. |

For the briefing, the code takes up to four recent records from each of portfolio, novelty, attempt, research review, critic, and legacy import. It adds open issues and recently assigned families. Excerpts have explicit IDs and truncation markers. You can do the same by copying recent cards and labeling any shortened card “excerpt; complete record available as ID ...”. Do not pretend the briefing contains the entire history.

**Whenever a research reply asks to look something up:** if `search_queries` or `read_requests` is nonempty, do not act on its proposed decision yet. Search all saved cards for the query terms, or open the requested ID. File what you found, append it to the original prompt under `REQUESTED ARCHIVE EVIDENCE (historical data):`, and ask that same role again. If there is too much text for one message, provide sections in order and label the next section clearly. The script uses 16,000-character sections of the complete stored record, starting at the requested character offset; the reply can request the next offset. It pauses after 12 retrieval exchanges in one step so a broken lookup loop cannot run forever. Empty search results mean only that those terms found nothing.

The result forms below are part of the assignment: with a basic chat model, append the supplied response instruction to the prompt. If it omits fields or returns contradictory statuses, ask it to correct the response before proceeding. The script validates these contracts and stops after repeated invalid responses. Never fill in missing mathematical evidence yourself just to make a form complete.

## 3. Ask the planner for several directions

Use the following prompt in a fresh conversation. On the first round, use the initial empty briefing and feedback described above.
"""]
    parts.append(prompt_block(render_template(ap[steps["propose"]["prompt"]], values), steps["propose"]["schema"]))
    parts.append("""
File the complete answer as a **portfolio** record. It must contain three to five proposals with different nonempty mechanisms once evidence requests are finished. A portfolio suggestion has not yet been tried. Keep the proposals in the returned order and select the first one for the next step.

## 4. Check one proposal against the full history before assigning it

Search your entire archive for its family, mechanism, and obstacle; read the matching attempts and failure cards. Put those results in `[RELATED RECORDS]`, preserving IDs. Use a fresh conversation for this assessor; do not tell it that it must approve the proposal.
""")
    parts.append(prompt_block(render_template(ap[steps["assess"]["prompt"]], values), steps["assess"]["schema"]))
    parts.append(f"""
File the **novelty** assessment even if it rejects the proposal. Then enforce these rules yourself:

1. `reject` means no research assignment for this proposal. Try the next portfolio proposal. If every proposal is rejected, return to step 3 with the updated records.
2. Do not assign an identical mechanism already assigned earlier. The script compares text after ignoring letter case and whitespace; the assessor additionally judges whether different wording hides the same idea.
3. Do not assign either of the {research['research']['family_cooldown']} most recently assigned families. A family may return only after two other families have been assigned. Apply this rule even if the assessor says `proceed` or `reopen`.
4. For `reopen`, require existing prior record IDs and concrete new evidence addressing the recorded reopening condition. A changed mechanism is still required; simply saying “try harder” is insufficient.
5. Reject references to record IDs that do not exist. Require nonempty mechanism and canonical family names.

If all checks pass, create an **assignment** card with the exact proposal, assessor decision ID, canonical family, relevant earlier records, and current round. Add its family to the recently assigned list. Set the next step in `STATE.md` to “explore this assignment.” File this assignment **before** asking the researcher to work.

This separates a failed claim from a whole family. For example, a counterexample to “make any locally legal choice” blocks that rule; it does not prove that every method using sequential choices fails. The assessor must compare the actual obstacle and the proposed new mechanism.

## 5. Give the researcher exactly that registered assignment

Use the exact assigned proposal and its retrieved evidence. Use a fresh conversation.
""")
    parts.append(prompt_block(render_template(ap[steps["explore"]["prompt"]], values), steps["explore"]["schema"]))
    parts.append("""
File the entire reply as an **attempt** card, including work, evidence, remaining obligations, and next test. Preserve a complete candidate argument when one is returned. Set the next step to “review this result.” An incomplete result is a useful completed research round; it is not the end of the overall task. Do not let the researcher silently replace the assigned mechanism with another one.

## 6. Independently review and file what this attempt established

Open another fresh conversation. Paste the full result into `[CURRENT RESULT]`.
""")
    parts.append(prompt_block(render_template(ap[steps["review"]["prompt"]], values), steps["review"]["schema"]))
    parts.append("""
File the reply as a **research_review** card. Link it from `FAILED.md` if it is refuted or incomplete. Link any reviewed reusable result from `PROVED.md`, keeping its assumptions and remaining limitations visible.

- If the verdict is **refuted**, require checked evidence and record precisely which claim it refutes. Preserve the reopening condition. Return to step 3 for a new portfolio informed by this result.
- If the verdict is **incomplete**, preserve the exact missing argument and next useful test. Return to step 3.
- Advance only if **both** researcher and reviewer say **candidate**, the researcher supplied the full argument, and that exact candidate has not already been submitted. An identical previously submitted candidate returns to planning; changing only its label is insufficient.

When advancing, preserve the full candidate and its status “awaiting critic.” Save a working copy as `saved-candidate.md`, while retaining the original result card. Continue to step 7.

Notice the loop: **plan → assess novelty → register → explore → review → plan again**. The worker runs this loop; the LLM does not have to sustain a single endless conversation. After a research review the planner creates a fresh portfolio using the new evidence; the unused old suggestions remain recorded as suggestions.

## 7. Give the same candidate to three independent auditors

Start three separate conversations. You can run them one after another if you have only one chat window; keep their contexts separate and do not show one auditor another's answer. All receive the same exact candidate. The assigned focus differs.
""")
    for number, focus in enumerate(audit["items"], 1):
        parts.append(f"\n### Auditor {number}\n\n")
        audit_values = {"focus": focus, "instructions": _instructions(audit, ap),
                        "statement": "[STATEMENT]", "solution": "[CANDIDATE]"}
        parts.append(prompt_block(render_template(ap[audit["prompt"]], audit_values), audit["schema"]))
        parts.append(f"\nFile auditor {number}'s full reply immediately. Preserve its assigned focus even if the model changes the label.\n")
    parts.append("""
Do not proceed without all three completed reports. The automated run saves each one to its archive and checkpoints the collection in `critic-audits.json`. If interrupted, reuse only reports for this exact candidate and the same audit settings. A changed candidate needs three fresh audits.

## 8. Ask the coordinating critic to decide and, if possible, repair

Put the three actual replies into `[THREE AUDITS]` in their original order. Supply the complete current candidate and the list of existing open issues. Use a fresh conversation.
""")
    critic_values = {"instructions": _instructions(critic, ap), "audits": "[THREE AUDITS]",
                     "statement": "[STATEMENT]", "solution": "[CANDIDATE]", "obligations": "[OPEN OBLIGATIONS]"}
    parts.append(prompt_block(render_template(ap[critic["prompt"]], critic_values), critic["schema"]))
    parts.append("""
File the full **critic** record against the exact candidate it reviewed. If it supplies a changed solution, file that replacement too and link it to the reviewed version. Then update `STATE.md` and the failure/result indexes.

Give every newly unresolved obligation a stable ID. The script derives `issue-...` IDs from the issue text; manually you may use `issue-001`, `issue-002`, and so on. Reuse an existing ID for the same obligation. Remove an issue from the open list only when `resolved_obligations` names that existing ID and supplies explicit evidence of resolution or non-applicability to this candidate. Apply that closure only after an independent review of the unchanged candidate; a changed candidate must be checked again first. A review may close some issues while rejecting the candidate for other still-open issues. Silence about an old issue does not close it. Unknown IDs or empty evidence require a corrected reply.

Follow these routing rules:

1. **Reject:** preserve its best safely repaired candidate and exact remaining bugs; use the complete report as `[LATEST CRITIC FEEDBACK]`, and return to step 3. The next planning round must account for those bugs. The critic-round counter resets.
2. **Changed solution with pass:** compare the returned text with the input yourself; the script checks the normalized text rather than trusting `fixed`. Any substantive change goes back to step 7 with three fresh auditors. After the configured consecutive repair-round limit (default 2), pause as **verification incomplete** and preserve the candidate. Do not call it accepted.
3. **Unchanged solution with pass:** advance only when all three actual audits pass, bugs is empty, no newly unresolved obligations are reported, and all earlier open obligations have been explicitly resolved. Otherwise it has not passed.

Treat the auditor reports as the actual three recorded answers even if the coordinator paraphrases or changes its `checks` field. The program replaces that field with the original reports before making its decision.

## 9. Save the accepted version, then ask for readable LaTeX

Keep the exact accepted text separately before editing. In a normal proof job the program stores the statement and accepted solution in `final-input.json`; the formatted candidate must not replace this input. Use a fresh conversation and paste the accepted text in `[ACCEPTED SOLUTION]`.
""")
    editor = cleanup["nodes"]["latex_editor"]
    edit_values = {"instructions": _instructions(editor, cp), "statement": "[STATEMENT]", "solution": "[ACCEPTED SOLUTION]",
                   "feedback": "[FINALIZATION FEEDBACK]"}
    parts.append(prompt_block(render_template(cp["proof_input"], edit_values), editor["schema"]))
    parts.append("""
File the complete reply and save its `latex` text as `formatted-candidate.tex`. LaTeX is a document format; at this point the document is only a formatting candidate. Do not announce a final result yet.

## 10. Independently check that editing did not change the argument

Use a fresh conversation with the original accepted solution and the entire formatted document.
""")
    verifier = cleanup["nodes"]["final_verifier"]
    verification_values = {"instructions": _instructions(verifier, cp), "statement": "[STATEMENT]",
                           "original": "[ACCEPTED SOLUTION]", "formatted": "[FORMATTED LATEX]"}
    parts.append(prompt_block(render_template(cp[verifier["prompt"]], verification_values), verifier["schema"]))
    parts.append("""
File the verdict. If it is **reject**, stop this finalization attempt with the stated issues, keeping both versions. A resumed finalization returns to step 9 with the unchanged accepted source and these reports in `[FINALIZATION FEEDBACK]`; it must pass the checks again. If it is **pass**, continue to the local document check.

## 11. Compile when available, then deliver with the actual verification status

Save the exact LaTeX to `final-validation/main.tex`. If `pdflatex` is installed, compile in that folder with shell execution disabled and a 30-second timeout. The program also restricts file access and saves the entire compiler output as `final-validation/compile.log`.

- Compilation failure or timeout: preserve the document and log; report that finalization is incomplete.
- Compiler unavailable: deliver the content-reviewed LaTeX and explicitly say compilation was unavailable. Do not claim that a PDF was compiled.
- Compilation passes and produces a PDF: deliver the reviewed LaTeX and compiled PDF.

A model review can miss a mathematical error. The workflow's acceptance means the exact document passed these recorded checks; it is not formal proof verification or a guarantee of correctness.

## 12. Continue later from the recorded next step

Before stopping, make sure `STATE.md` identifies the pending step and all necessary record IDs. Preserve the full folder. Next time, read the exact statement, current checkpoint, recent briefing, and relevant older records; then execute the pending step. Never restart from an empty notebook for the same statement.

In the program, continuation creates a new run folder, takes a consistent copy of `research.sqlite3`, and rebuilds the readable views. It also carries historical notebooks forward; legacy claims are imported as evidence that still needs checking. Prompt/model changes are recorded as new versions without erasing prior history. Changing the exact statement requires a separate archive.

```bash
python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN
```

This starts a browser-visible continued job with a new time budget, using the saved settings unless you override them. A completed request can be reused; a half-returned request is retried from its saved input. The permanent archive contains public prompts, returned text, explicit evidence, decisions, and checkpoints, not inaccessible private internal model reasoning.

The repeat checks enforce exact-mechanism blocking and family rotation; the independent assessor checks deeper similarity. A model can still misclassify related ideas or miss a failure. The safeguards make persistence and diversity explicit and reviewable, but cannot promise an endless supply of useful discoveries.

---

This manual is generated from the bundled YAML prompts and response contracts. Refresh or check it with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/render_manual.py --check
```
""")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the saved manual differs from the current YAML")
    arguments = parser.parse_args()
    target = ROOT / "docs/manual_workflow.md"
    generated = render()
    if arguments.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != generated:
            raise SystemExit("Manual is out of date; run python3 docs/render_manual.py")
        print("Operator manual matches current workflow prompts and response contracts.")
    else:
        target.write_text(generated, encoding="utf-8")
        print(target)


if __name__ == "__main__":
    main()
