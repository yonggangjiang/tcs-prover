"""Every text TCS Prover sends to a model, as the Prompts dialog lists them (GET /prompt-catalog).

The texts themselves live in the workflows' prompts (workflows/author_critic.yaml, workflows/clean_up.yaml) and, for
the statement reviewer, in ui/review.py; this module names them, groups them, says when each is used and which
placeholders it may use, and checks an edit as the runner checks it before a launch (workflow_runner.prompt_rules()).

A catalog name is the key of a job's promptOverrides and promptSet. The per-job prompts that existed before keep their
keys (review, author, coordinator, critic, convention, final); every other text uses its name in the workflow.
"""

GROUPS = ("Statement review", "Author", "Coordinator", "Goal loop", "Team", "Team tools", "Critic", "Writer", "Other")

# Catalog names whose workflow prompt has another name, and the texts that live outside the workflows.
WORKFLOW_NAMES = {"convention": "convention_critic"}
REVIEW = "review"

# (name, group, label, when it is used), in display order. A workflow prompt not listed here is shown in "Other".
ENTRIES = (
    (REVIEW, "Statement review", "Statement reviewer",
     "The instructions of the statement review (Review step), before the statement and any revision request."),
    ("author", "Author", "Author prompt",
     "The proof author's first prompt when it works alone (Author subagents 0). [STATEMENT] is replaced by the "
     "statement."),
    ("goal", "Author", "Author goal",
     "The author's goal: the objective of Codex's goal mode (Goal loop: Codex), the {objective} of the goal "
     "continuation (Goal loop: TCS Prover) and of Claude Code's goal note."),
    ("continuation", "Author", "Author continuation",
     "Sent after an author turn that did not end the goal: placed first in the goal continuation (TCS Prover's goal "
     "loop); with Codex's goal mode, only when Codex does not continue by itself; Claude Code gets it as it is."),
    ("reopen", "Author", "Reopened conversation",
     "The first message when a paused, stopped or restarted author's saved conversation is reopened."),
    ("compaction", "Author", "After compaction",
     "Sent into the running turn after the author's conversation was compacted."),
    ("resume", "Author", "New conversation",
     "The first message when the author continues in a new conversation (its saved one is gone or too long), or "
     "after a reply turn compacted it."),
    ("repair", "Author", "Critic rejection",
     "Sent after the critic rejects the author's proof: {solution} is the critic's candidate, {bugs} its findings."),
    ("handoff_refused", "Author", "Refused hand-in",
     "Added to the next message when the author's last reply could not be handed to the critic; {reason} says why."),
    ("summary", "Author", "Progress summary",
     "The paused author's progress summary (Pause and summarize, and when the time limit is reached)."),
    ("conversation", "Author", "Reply to a message",
     "The paused author's reply to a chat message ({message})."),
    ("chat", "Author", "Chat message",
     "Frames a chat message sent to the working author or coordinator: {message}, and {answer_file} for an answer."),
    ("related_works", "Author", "Related works",
     "Appended to the first prompt when the job has related works."),
    ("subagents", "Author", "Subagents note",
     "Appended to the first prompt of an author that works alone with subagents (jobs from before the coordinator, "
     "terminal runs with --author-mode author); {author_subagents} is their number."),
    ("coordinator", "Coordinator", "Coordinator prompt",
     "The first prompt of the author coordinator (Author subagents 1 or more). {author_subagents} is the number of "
     "author subagents; [STATEMENT] is replaced by the statement."),
    ("coordinator_goal", "Coordinator", "Coordinator goal", "Replaces the author goal for the coordinator."),
    ("coordinator_continuation", "Coordinator", "Coordinator continuation",
     "Replaces the author continuation for the coordinator."),
    ("coordinator_reopen", "Coordinator", "Coordinator reopened conversation",
     "Replaces the reopened-conversation message for the coordinator."),
    ("coordinator_compaction", "Coordinator", "Coordinator after compaction",
     "Replaces the after-compaction message for the coordinator."),
    ("coordinator_resume", "Coordinator", "Coordinator new conversation",
     "Replaces the new-conversation message for the coordinator."),
    ("coordinator_repair", "Coordinator", "Coordinator critic rejection",
     "Replaces the critic-rejection message for the coordinator."),
    ("coordinator_summary", "Coordinator", "Coordinator progress summary",
     "Replaces the progress summary for the coordinator."),
    ("coordinator_checkin", "Coordinator", "Time check",
     "Sent to the working coordinator every hour of working time: {elapsed} and {limit}."),
    ("coordinator_report", "Coordinator", "Answer to a report",
     "Sent in place of the continuation when the coordinator's final reply names no proof file (it is kept as a "
     "progress report)."),
    ("goal_continuation", "Goal loop", "Goal continuation",
     "Goal loop TCS Prover: the next turn after every turn of a Codex author or coordinator that did not end its "
     "goal. {continuation} is the continuation (or the answer to a report), {objective} the goal, {elapsed} the "
     "working time and {limit} the time limit. Default: Codex's own continuation text."),
    ("goal_tool_update_goal", "Goal loop", "update_goal tool",
     "Goal loop TCS Prover: the description of the update_goal tool with which a Codex author or coordinator ends "
     "its goal (complete hands its final reply on; blocked and paused are recorded and the work goes on). Default: "
     "Codex's own."),
    ("goal_tool_update_goal_status", "Goal loop", "update_goal: status",
     "The description of update_goal's status argument. Default: Codex's own."),
    ("goal_tool_update_goal_note", "Goal loop", "update_goal: note",
     "The description of update_goal's optional note, shown on the job page."),
    ("claude_goal_harness", "Goal loop", "Claude Code goal note",
     "Added to the system prompt of a Claude Code author or coordinator, which ends every turn with a goal status; "
     "{objective} is the goal."),
    ("codex_compact_prompt", "Goal loop", "Codex compaction summary",
     "Codex's compact_prompt setting for Codex authors, coordinators and team members: the instructions for the "
     "summary Codex writes when it compacts a conversation. Empty: Codex's own."),
    ("team_note", "Team", "Team note",
     "Appended to the coordinator's first prompt when its subagents are TCS Prover's team; {members} lists them."),
    ("team_member", "Team", "Member assignment",
     "What a team member receives for an assignment or a follow-up; {assignment} is the coordinator's text."),
    ("team_tool_members", "Team tools", "team_members tool", "The description of team_members."),
    ("team_tool_assign", "Team tools", "team_assign tool", "The description of team_assign."),
    ("team_tool_assign_member", "Team tools", "team_assign: member", "The description of team_assign's member."),
    ("team_tool_assign_assignment", "Team tools", "team_assign: assignment",
     "The description of team_assign's assignment."),
    ("team_tool_wait", "Team tools", "team_wait tool", "The description of team_wait."),
    ("team_tool_wait_timeout_minutes", "Team tools", "team_wait: timeout_minutes",
     "The description of team_wait's timeout_minutes."),
    ("team_tool_followup", "Team tools", "team_followup tool", "The description of team_followup."),
    ("team_tool_followup_member", "Team tools", "team_followup: member", "The description of team_followup's member."),
    ("team_tool_followup_message", "Team tools", "team_followup: message",
     "The description of team_followup's message."),
    ("team_tool_stop", "Team tools", "team_stop tool", "The description of team_stop."),
    ("team_tool_stop_member", "Team tools", "team_stop: member", "The description of team_stop's member."),
    ("critic", "Critic", "Critic instructions",
     "The critic's instructions in every critic round; {count} is the number of independent critics."),
    ("critic_verify", "Critic", "Final verification",
     "Added to the critic's instructions in its last round, when it may no longer edit the proof."),
    ("critic_input", "Critic", "Critic request",
     "Frames the critic's request: {instructions} and the {statement}."),
    ("convention", "Critic", "Convention critic",
     "The convention critic's prompt, once on the accepted proof; {statement} is the statement."),
    ("final", "Writer", "Writer instructions", "The instructions of the paper writer (Writing step)."),
    ("solution_input", "Writer", "Writer request",
     "Frames the writer's request: {instructions}, the {statement} and the {solution}."),
    ("compilation_repair", "Writer", "LaTeX repair",
     "Sent when the paper does not compile: {diagnostic} and {compiler_output}."),
    ("compilation_timeout", "Writer", "LaTeX timeout repair",
     "Sent when the paper's compilation does not finish: {diagnostic} and {compiler_output}."),
)
_KNOWN = {entry[0] for entry in ENTRIES}


def workflow_name(name):
    """The workflow prompt a catalog name stands for."""

    return WORKFLOW_NAMES.get(name, name)


def catalog_name(prompt):
    """The catalog name of a workflow prompt."""

    return next((name for name, value in WORKFLOW_NAMES.items() if value == prompt), prompt)


def _placeholders(rule):
    fields, marker = rule
    return [*([marker] if marker else []), *sorted("{" + field + "}" for field in (fields or ()))]


def entries(runtime, review_prompt):
    """The catalog: [{name, group, label, when, placeholders, template, text, emptyAllowed}] with the current defaults.

    ``runtime`` is workflow_runner (its built-in workflows, read as a launch reads them, and prompt_rules());
    ``review_prompt`` the statement reviewer's default instructions. ``template`` says whether the text is filled
    in like a template, where a brace that is not a placeholder is written twice: true for every text with
    placeholders and for the progress summaries, which have none; false for a text used as written, braces included.
    """

    texts, rules, order = {REVIEW: review_prompt}, {REVIEW: (None, None)}, []
    for workflow_file in ("author_critic", "clean_up"):
        workflow = runtime.builtin_workflow(workflow_file)
        for prompt, rule in runtime.prompt_rules(workflow).items():
            name = catalog_name(prompt)
            if name not in texts:
                texts[name], rules[name] = workflow["prompts"][prompt], rule
                order.append(name)
    listed = [name for name, *_ in ENTRIES if name in texts]
    others = [name for name in order if name not in _KNOWN]
    described = {name: (group, label, when) for name, group, label, when in ENTRIES}
    return [{"name": name, "group": described.get(name, ("Other", name, "A prompt of the workflow."))[0],
             "label": described.get(name, ("Other", name, ""))[1],
             "when": described.get(name, ("Other", name, "A prompt of the workflow."))[2],
             "placeholders": _placeholders(rules[name]), "template": rules[name][0] is not None, "text": texts[name],
             "emptyAllowed": name in runtime.EMPTY_ALLOWED_PROMPTS}
            for name in [*listed, *others]]


def rules(runtime):
    """{catalog name: (fields, marker)} of every text (prompt_rules() of both workflows; the review is plain)."""

    found = {REVIEW: (None, None)}
    for workflow_file in ("author_critic", "clean_up"):
        for prompt, rule in runtime.prompt_rules(runtime.builtin_workflow(workflow_file)).items():
            found.setdefault(catalog_name(prompt), rule)
    return found


def problems(runtime, overrides):
    """{name: why it cannot be used} for the edited texts ``overrides`` ({catalog name: text}); {} when all are fine."""

    known = rules(runtime)
    if not isinstance(overrides, dict):
        return {"": "Prompt overrides must map prompt names to text."}
    found = {}
    for name, text in overrides.items():
        if name not in known:
            found[str(name)] = "There is no prompt of this name."
            continue
        problem = runtime.prompt_problem(workflow_name(name), text, known[name])
        if problem:
            found[name] = problem
    return found


def label(name):
    """The label of a catalog name, for messages."""

    return next((entry[2] for entry in ENTRIES if entry[0] == name), name)
