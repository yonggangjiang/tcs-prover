# Writing workflows

`workflow_runner.py` executes a YAML graph. The YAML owns prompts, response
shapes, result checks, state updates, and routing. The runner supplies model
calls and persistent author sessions. Node names carry no special meaning;
the `run` operation selects the controller behavior.

The bundled `workflows/` directory contains only `author_critic.yaml` and
`clean_up.yaml`. Keep your own definitions elsewhere, for example
`summarize.yaml` beside the root launcher. The first two examples are complete
files; their commands make model calls. The [offline example](#check-a-workflow-offline)
shows how to exercise them without a model.

## Start with one node

Save this as `summarize.yaml`:

```yaml
nodes:
  summarize:
    run: structured
    prompt: summarize
    inputs:
      content: state.input
    response:
      summary: string
    after:
      - set: {output: result.summary}
    next: end
prompts:
  summarize: |-
    Summarize the following text in one sentence.

    TEXT: {content}
```

```bash
python3 workflow_runner.py summarize.yaml < notes.md
```

The first node is the entry point. `inputs` fills its prompt fields from shared
state. `response` requests a JSON object containing a string named `summary`.
The `after` action copies that response into `state.output`. `next: end` finishes
the graph after its default outcome, `done`.

The CLI writes JSONL events. Its final `workflow_result` event contains the
value of `state.output`; it does not print the result as an unwrapped document.

## Add decisions and repetition

Save this as `edit_note.yaml`. The editor can accept, revise, or stop; the
publisher runs after acceptance or the editing limit:

```yaml
nodes:
  editor:
    run: structured
    role: editor
    stage: edit
    prompt: edit
    inputs:
      note: get(state, 'output', state.input)
    response:
      text: string
      decision: [ready, revise, stop]
    require:
      - is_string(result.text) and bool(strip(result.text))
    error: The editor returned an empty note.
    outcome: result.decision
    after:
      - set: {output: 'strip(result.text)'}
    next:
      ready: publisher
      revise: {repeat: 2, option: editing_rounds, then: publisher}
      stop:
        to: end
        after:
          - set: {failed: 'True'}
  publisher:
    run: structured
    stage: publish
    prompt: publish
    inputs:
      note: state.output
    response:
      headline: string
      body: string
    after:
      - set:
          output: "result.headline + '\\n\\n' + result.body"
    next: end
prompts:
  edit: |-
    Shorten this note without changing its meaning. Return the edited text.
    Set decision to ready if it is publishable, revise if another editing
    pass would help, or stop if the source is too incomplete to edit.

    NOTE: {note}
  publish: |-
    Give this note a short headline and return its body without changing
    the facts.

    NOTE: {note}
```

```bash
python3 workflow_runner.py edit_note.yaml --set editing_rounds=3 < note.md
python3 workflow_runner.py edit_note.yaml --set editor_model=gpt-6-astra < note.md
```

`visit` starts at 1 each time execution enters a different node. A self-loop
increments it; leaving the node resets it. A repeat limit counts all consecutive
visits to that node, including the first, rather than counting only one outcome.
At `visit >= limit`, the repeat branch takes `then`; earlier visits repeat the
current node. Other outcomes keep their own routes even at the limit. There is
no implicit overall graph-iteration limit.

The built-in critic uses this same mechanism. A clean pass exits immediately,
rejection returns to the author and resets the critic's count, and the default
second repaired round saves verification incomplete rather than accepting an
unchecked repair.

## Reading the bundled workflows

For an author-only operator explanation with the exact prompts and no coding
prerequisites, read the [manual worker guide](../docs/manual_workflow.md). That
guide is regenerated from the author YAML using `docs/render_manual.py`.

In [`author_critic.yaml`](author_critic.yaml), `author` uses `run: goal`.
Its `task` reads `state.statement`, and the full author prompt contains the
`[STATEMENT]` marker. One author session explores, checks, records, and revises
its ideas. Its only persistent author memory is `INITIAL_PROMPT.md`,
`APPROACHES.md`, and `PROVED.md`. The model writes the latter two; the runner
initializes absent files and preserves the complete original prompt unchanged.
There is no separate planner/assessor/result-reviewer sequence inside this node.

The author searches its own detailed attempt history before choosing a route,
records mechanisms, obstacles, failed steps and reopening conditions, and keeps
only rigorously proved positive or negative lemmas in `PROVED.md`. These are
prompt-directed responsibilities of the author, not a controller-enforced
semantic novelty test. The same session resumes after ordinary continuation,
compaction, or critic feedback. When that session is unavailable, a fresh one
reads the three files before continuing the same task.

A full candidate yields `outcome: proof`; unsuccessful termination yields
`outcome: failure`. The YAML merges the result and sets `state.failed` to
prevent an incomplete search from starting finalization.

The critic saves `saved-candidate.md`, runs three fresh independent audits in
parallel, and checkpoints completed reports in `critic-audits.json`. The
coordinator consumes the actual reports. Before routing, the runner restores
those original reports in `checks`, compares candidate text to determine
whether it changed, and rejects passes that leave known obligations unresolved.
The YAML memory action records the review before replacing `state.solution`,
so reviewed and repaired versions stay linked.

A clean unchanged pass ends the proof stage. Rejection returns its latest
candidate and exact feedback to the same author. A changed proof gets a fresh
three-auditor round; the default `repeat: 2` limit produces **verification
incomplete**, preserves the latest candidate, and sets `state.failed`. It
never approves the last repair merely because the review budget was exhausted.

In [`clean_up.yaml`](clean_up.yaml), `latex_editor` saves the original input in
`state.accepted_solution` and, for proof jobs, `final-input.json`. It writes
`formatted-candidate.tex` and proceeds to `final_verifier`. This independent
structured call checks the original against the formatted content, including
expanded explanations. A rejection preserves the document and emits failure;
a pass runs the `verify_latex` action. Compilation failure also remains
incomplete. An unavailable compiler is reported explicitly while leaving the
content-reviewed document available. Only successful content verification and
a non-failing compilation status permit `final_result`. Resumed finalization
retries the editor with the original source and saved verification/compiler
feedback.

| Bundled prompt name | Purpose |
| --- | --- |
| `author` | Full initial statement and search/memory strategy. |
| Goal lifecycle prompts | Continue, reread after compaction, resume after interruption, or repair after critic feedback. |
| `critic`, `critic_memory` | Coordinate audits/repairs and preserve historical obligations with evidence. |
| `critic_input`, `critic_audit` | Assemble the actual coordinator and auditor inputs. |
| `final`, `proof_input`, `source_input` | Edit an accepted proof or existing theorem/proof source. |
| `verify_final`, `verification_input` | Independently check preservation of the final argument. |

The author prompt and lifecycle/file bindings are part of the YAML. Changing
the prompt does not require adding a new planner or a database service.

## Root entries and shared node entries

A file has exactly two root mappings, in either order:

| Root entry | Meaning |
| --- | --- |
| `nodes` | Nonempty ordered mapping of node names to definitions. The first node runs first. `end` is reserved as the terminal target. |
| `prompts` | Mapping of names to nonempty strings. Nodes reference these names. |

Use YAML `|-` for multiline prompt text without a final newline, or `|` to
retain one.

Every node requires `run`, `prompt`, and `next`. Unknown node fields are rejected.

| Shared entry | Meaning and default |
| --- | --- |
| `run` | Required operation: `structured` or `goal`. |
| `prompt` | Required name in `prompts`. Structured nodes also support the conditional form below. |
| `next` | Required transition string or outcome mapping; see [transitions](#transitions). |
| `role` | Optional model-setting namespace, such as `editor`, `author`, or `critic`. It does not choose an operation. |
| `model` | Optional model default for this node. |
| `effort` | Optional reasoning-effort default for this node. |
| `stage` | Event stage for the node's hooks and structured request. Without it, structured requests and node `after` events use `model`; `before` and transition events use the node name. Goal session stages have a separate `stages` mapping. |
| `outcome` | Expression returning a string present in `next`; defaults to the expression `'done'`. |
| `before` | Ordered actions before each visit's model/session call; defaults to `[]`. |
| `after` | Ordered actions after response checks and outcome selection on each visit; defaults to `[]`. |

Model selection uses the first available value in this order:
`options.<role>_model`, node `model`, global `options.model`, then `gpt-6-astra`.
Effort follows `options.<role>_effort`, node `effort`, global `options.effort`,
then `ultra`. `--model` and `--reasoning-effort` set the global options.
Generation speed is a global `speed` option, defaulting to `fast`. DeepSeek
uses standard speed and normalizes effort to its supported `high`/`max` levels.
It requires `DEEPSEEK_API_KEY`; see the [setup guide](../README.md#how-to-use-deepseek).
The `summary` option selects public activity summaries (`none`, `concise`, or
`detailed`), defaulting to `concise`.
Run `python3 workflow_runner.py --help` for the supported model and effort names.

## Structured nodes

Each `structured` visit starts a fresh structured model call. These entries are
specific to that operation:

| Entry | Meaning and default |
| --- | --- |
| `response` | Compact description of the requested response object. Use either this entry or `schema`. |
| `schema` | Full JSON Schema object supplied to the model; alternative to `response`. |
| `inputs` | Mapping from prompt field names to expressions evaluated against `state` and `visit`; defaults to `{}`. |
| `instructions` | One prompt name or a list of names, joined in order with blank lines and supplied as `{instructions}`. Defaults to an empty string. Instruction text already present in the accumulated instructions is not appended again. |
| `features` | Optional additional Codex capabilities for the structured call; defaults to `[]`. Structured transport normally disables shell, web, image, and subagent tools. |
| `parallel` | Optional independent requests collected before the parent request; see below. |
| `attempts` | Positive maximum number of transport/structured-output attempts. The default is 2; the bundled auditors and coordinator explicitly use 1. |
| `provider_options` | Provider-specific `effort` and/or positive `timeout` (seconds), for example `{deepseek: {effort: high, timeout: 1800}}`. These override resolved request settings. |
| `request_label`, `activity_label` | Optional transport labels. `activity_label` defaults to `request_label` when the latter is set. |
| `require` | List of expressions checked locally against the returned result; every expression must be truthy. Defaults to `[]`. |
| `error` | Error message for a failed `require` check or outcome-expression evaluation. Without it, the underlying diagnostic is used. |

The schema is checked before execution. Returned JSON must be an object and
passes local JSON Schema validation before the node's `require` checks run.
OpenAI requests also supply the schema to the provider. DeepSeek receives the
JSON contract in its prompt and relies on the local validator. `require` adds
relations between fields, such as requiring an empty bugs list for acceptance.
Invalid structured output may be retried up to `attempts`; the proof critic's
one-attempt setting prevents an automatic repeat of an expensive audit.

### Response shorthand

`response` is a mapping of field names to descriptors. It expands to a JSON
Schema object with every field required and additional fields disallowed.
Nested objects follow the same rule.

| Descriptor | Meaning |
| --- | --- |
| `string`, `boolean`, `integer`, `number`, `"null"` | Scalar JSON type. Quote `"null"` so YAML does not interpret it as an empty value. |
| `[pass, reject]` | Nonempty enum of string values. Quote values such as `"yes"`, `"no"`, or `"null"` that YAML would otherwise convert. |
| `{fields: {name: string, count: integer}}` | Nested object. |
| `{items: string}` | Array of values matching its item descriptor. |
| `{items: ..., minItems: 3, maxItems: 3}` | Array with optional nonnegative integer bounds; the minimum cannot exceed the maximum. |

For example:

```yaml
response:
  verdict: [pass, reject]
  checks:
    items:
      fields:
        focus: string
        passed: boolean
    minItems: 3
    maxItems: 3
```

Use full `schema` for constraints outside this shorthand. `response` and
`schema` cannot be combined on the same node.

### Prompt selection and substitution

A structured prompt may select between two prompt names:

```yaml
prompt:
  when: "'solution' in state"
  then: proof_input
  else: source_input
```

`when` sees `state` and `visit`. All `inputs` expressions are evaluated before
the prompt is selected, so guard any field that may be missing, for example
`state.solution if 'solution' in state else ''`.

Template placeholders refer to `inputs` names or `instructions`. Dictionary
fields such as `{record.title}` are supported. Literal braces must be doubled
as `{{` and `}}`; format specifications, conversions, and indexing inside
placeholders are unsupported. Inserted values are not formatted again, so braces
inside supplied text remain literal. Instruction prompt text is inserted as
text rather than recursively templated.

### Parallel requests and checkpoints

A structured node can gather independent results before its own request. The
`parallel` block describes those child requests. Its `inputs` additionally see
`item`, the current entry from `items`. After they all finish, the parent
`inputs` can use `parallel`, a list of results in the original item order.
For example, `audits: json(parallel)` supplies them to a `{audits}` placeholder.
A failed child prevents the parent request from starting.

| Parallel entry | Meaning |
| --- | --- |
| `items` | Required nonempty list of literal input values, one per independent request. |
| `prompt` | Required named prompt for each child. |
| `inputs`, `instructions`, `response` or `schema` | Same request fields as a structured node; `inputs` can also read `item`. |
| `role`, `stage`, `model`, `effort` | Inherit from the parent when omitted. Explicit child values follow the same role-option precedence. |
| `features`, `attempts`, `provider_options` | Child-specific request settings; optional. |
| `item_field` | Optional result field replaced with the original input item, preventing the model from changing that association. |
| `output` | Optional parent-result field replaced with the canonical child-result list. The resulting parent object is validated again. |
| `checkpoint` | Optional reusable-results configuration described below. |
| `run` | Optional compatibility entry; only `structured` is supported for child requests. |

The child descriptor does not contain `next`, `outcome`, or state actions.
The parent owns those operations. The bundled critic uses three child audits,
then a coordinator, with `output: checks` preserving the actual auditor records.

A checkpoint block has these fields:

| Checkpoint entry | Meaning |
| --- | --- |
| `file` | Required checkpoint filename within the current workspace. |
| `identity` | Required mapping of identity fields to expressions using `state`, `visit`, and the joined `instructions`. Include all inputs that determine whether prior results remain applicable. |
| `item_key` | Name used for the item list in the identity fingerprint; defaults to `items`. The built-in critic uses `focuses` for compatibility with existing runs. |
| `disabled` | Optional marker filename that disables searching sibling run folders; defaults to `fresh-critic-audits`. It does not suppress a matching checkpoint already in the current workspace. |

The fingerprint combines the declared identity, selected model, effective
effort, and item list. Each completed result is saved atomically, so a resumed
batch runs only its missing items. Stored results must satisfy the current child
schema. When the workspace is an immediate child of `runs/`, recovery also
searches sibling runs for matching assignments unless the marker is present.
For custom workflows, change the declared identity when changing instructions
or other assumptions that make existing results obsolete.

## Goal nodes

`run: goal` keeps one author session per node across visits. The first visit
starts the goal; later visits resume that same session with feedback from
shared state. The default author uses this operation with exactly three
Markdown memory files. It does not run separate planning, novelty-assessment,
or research-review model calls and does not maintain an author SQLite journal.

| Entry | Meaning and default |
| --- | --- |
| `task` | Required expression selecting nonempty task text from `state` and `visit`. |
| `marker` | Required literal appearing exactly once in the main prompt; replaced with the exact task. |
| `prompt_file` | Required filename present in `files`, containing the full permanent initial prompt. The default is explicitly `INITIAL_PROMPT.md`. |
| `files` | Required nonempty mapping of plain filenames to initial-content templates. Paths and directory traversal are not allowed. Templates receive `original_prompt` and `statement`; existing files are preserved. |
| `lifecycle` | Bindings for exactly `goal`, `continuation`, `compaction`, `repair`, and `resume`. Omission uses the same-named prompts; a list of all five names or a mapping to other named prompts is supported. |
| `resume` | Required expression mapping containing `solution`, `bugs`, and `round`, used to return critic feedback to the same session. |
| `stages` | Optional mapping with exactly `initial`, `resume`, and `failure`. Defaults to `{initial: solve, resume: repair, failure: failure}`. |
| `recovery` | Optional `{when, prompt}` for first activation entered with saved critic feedback. The named prompt is rendered with the recovery values. |

The bundled author declares its memory files directly:

```yaml
run: goal
prompt: author
task: state.statement
marker: '[STATEMENT]'
prompt_file: INITIAL_PROMPT.md
files:
  INITIAL_PROMPT.md: '{original_prompt}'
  APPROACHES.md: |
    # Attempted approaches

    No approaches attempted yet.
  PROVED.md: |
    # Proved lemmas

    No lemmas established yet.
lifecycle: [goal, continuation, compaction, repair, resume]
resume:
  solution: state.solution
  bugs: state.report.bugs
  round: state.round
```

This is an excerpt from a node; add the normal `next`/outcome/actions and
referenced prompts to form a complete graph. `original_prompt` is the entire
main prompt after statement-marker substitution. The runner saves that content
unchanged in the permanent instruction file. The author itself updates
`APPROACHES.md` and `PROVED.md` using file tools.

The initial author prompt specifies the recording procedure: give each attempt
a stable approach ID, save it before investigating, retain its detailed work,
mark it OPEN or CLOSED, and explain the exact obstacle and reopening condition.
Only fully justified lemmas with complete proofs belong in `PROVED.md`, linked
to their associated approach IDs. Negative lemmas may prove an obstruction or
counterexample to a specific claim; a missing argument is not such a proof.
Invalidated lemmas lose their proved status, with their error and correction
preserved in the associated approach and dependent results rechecked.

Before a new attempt, the author searches both notebooks for related mechanisms
and obstacles. Avoiding cosmetic repeats and choosing substantially different
directions are explicit prompt responsibilities. The controller does not
pretend to decide mathematical equivalence mechanically. There is no fixed
family cooldown or separate novelty gate.

The five lifecycle prompts have these uses:

| Lifecycle key | When used |
| --- | --- |
| `goal` | Objective installed for the existing persistent author task. |
| `continuation` | Continue the same session after an unfinished author turn while time remains. |
| `compaction` | Reread the permanent assignment and both notebooks after context compaction. |
| `repair` | Return the latest candidate and exact critic bugs to the same author. |
| `resume` | Resume preserved files in the current run folder, reusing the prior conversation when available. |

Lifecycle/recovery templates may reference `original_prompt`, `directory`,
`prompt_file`, `solution`, `bugs`, `round`, `revision_number`, and `instruction`.
The bundled `resume` uses `directory`; `repair` uses `round`, `solution`, and
`bugs`. Initial file templates use only `original_prompt` and `statement`.
There is no extra model call to summarize the files at a stopping boundary.

An author candidate yields `outcome: proof` and the complete `solution`.
Unsuccessful termination yields `outcome: failure` and a diagnostic in
`output`. Use `outcome: result.outcome` and `after: [{merge: result}]` to expose
this result to the graph, then set `state.failed` when failure should stop the
chain. The built-in author demonstrates these actions.

`thinking_hours`, `elapsed_seconds`, and `author_limit_file` control the shared
workflow deadline, which also bounds structured critic and final requests.
`author_steer_file` supplies live human instructions to the author session.
With multiple goal nodes, each node's configured files live in its own directory
under `goal-memory/`; the default one-author graph keeps its three files in the
run folder.

Continuation preserves the same conversation when the recorded thread is
available. If it cannot be resumed, a fresh thread reads the same three files.
The source run remains unchanged when the UI creates a continued run. Private
internal reasoning and a half-returned model response are not recovered as a
completed result.

```bash
python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN
```

The name of this continuation command is retained for compatibility; it now
continues the goal-based author and its three notebooks. The old `run: research`
operation, four research-step contracts, and ten-prompt author-ledger lifecycle
are no longer supported.

## State actions

Actions are ordered lists. `before` sees `state` and `visit`. Node `after` and
transition actions also see `result`, `raw`, and the selected `outcome`.
`raw` is the exact structured response text, or an empty string for a goal.
The repeat branch's terminal actions additionally see `limit`.

| Action entry | Behavior |
| --- | --- |
| `when` | Optional expression guarding the other operations in that action. A guard alone is invalid. |
| `set` | Mapping of state keys to expressions. All expressions in one mapping use the state before that mapping is applied; assignments are simultaneous. |
| `merge` | Expression producing a dictionary, merged into state. Existing keys are replaced. |
| `emit` | Event mapping requiring `kind`; may include `stage` and arbitrary event fields. String values are templates unless they begin with `=`, which evaluates the remaining expression and preserves its type. Non-string values pass through unchanged. |
| `memory` | Update the current durable candidate ledger, as described below. |
| `write` | `{path, text}` writes an expression result atomically to a literal path, relative to the working directory unless absolute. The expression must produce text; the file is replaced with private permissions where supported. |
| `verify_latex` | Evaluate LaTeX text and run a bounded local compilation check. Saves `final-validation/main.tex` and `compile.log`, sets `state.compilation` to status/diagnostic/engine, and marks `state.failed` on compilation failure. Missing `pdflatex` reports `unavailable` explicitly without claiming compilation passed. |

Use separate actions when an assignment must see an earlier assignment:

```yaml
after:
  - set: {output: 'strip(result.text)'}
  - set: {length: 'len(state.output)'}
  - emit:
      kind: edited_note
      label: 'Editing visit {visit}'
      output: =state.output
      length: =state.length
```

All values under `set`, `inputs`, and `resume` are **expression strings**.
For example, use `failed: 'True'` for a boolean and `status: "'accepted'"` for a
literal string. A mapping can contain several operations; their execution order
is `memory`, `merge`, `set`, `write`, `verify_latex`, then `emit`. Separate actions usually read more
clearly. The engine updates `state.outcome` after node `after` actions, so use
`outcome` directly inside those actions to refer to the current result.

### Durable memory action

`memory` does nothing if `state.memory` is absent or `None`. Otherwise it expects
the review-memory adapter (`ResearchMemory`) backed by the critic/final archive.
It is not a general dictionary store and does not maintain the author's three
notebooks. The default author writes its files directly; this action is used
by the critic to link reviewed and repaired candidate versions.

`memory: approved` sets the current candidate's status to that literal string.
The mapping form requires all five expression fields:

| Memory field | Value |
| --- | --- |
| `previous` | Candidate text before the review. |
| `candidate` | Candidate text after review or repair. |
| `feedback` | Review-result dictionary. The existing ledger understands `verdict`, `fixed`, `bugs`, and optional `memory_update`. |
| `source` | Label recording the source of changed candidate text. |
| `status` | Status for the resulting candidate. |

A changed candidate is recorded and linked to the reviewed attempt. The review
is recorded even when the candidate is unchanged. Historical `memory_update`
uses `approach_family`, `approach_result`, `blocked_routes` (entries with `route`,
`reason`, `reopen_condition`), and `unresolved_obligations`. Missing historical
metadata is tolerated for compatible callers. The critic also supplies
`resolved_obligations`, each with an existing issue `id` and concrete `evidence`;
omitted obligations remain open. The critic gate rejects a pass
with unresolved obligations before graph routing. Status/history actions
themselves do not choose graph routes;
the YAML's `next` does that.

## Transitions

`next: end` expands to `next: {done: end}`. More generally, `next: some_node`
routes the default `done` outcome to that node. Use a mapping for other outcomes:

| Branch form | Meaning |
| --- | --- |
| `ready: publisher` | Immediately enter `publisher`. |
| `ready: {to: publisher, after: [...]}` | Run branch actions, then enter `publisher`. |
| `revise: {repeat: 2, then: publisher}` | Repeat the current node while `visit < 2`; otherwise enter `publisher`. |
| `revise: {repeat: 2, option: editing_rounds, then: publisher, after: [...]}` | Take the limit from `options.editing_rounds` when provided. Run these branch actions only when the limit is reached, before entering `publisher`. |

Limits and their overrides must be positive integers. `after` actions run before
the destination node, including when that destination is `end`. The per-node
`after` list still runs on **every** completed visit. Outcomes missing from
`next` cause an error. The runner does not infer acceptance or failure from an
outcome's name.

## Expression reference

Expressions are interpreted as data operations; Python code is not compiled or
executed. Available context names depend on the location:

| Location | Names |
| --- | --- |
| Structured `inputs`, conditional `prompt.when`, goal `task` and `resume`, node `before` | `state`, `visit` |
| Parallel `inputs` | `state`, `visit`, `item`; parent `inputs` additionally see the completed `parallel` list. |
| Checkpoint `identity` | `state`, `visit`, `instructions` |
| Goal `recovery.when` | `state`, `visit` |
| `require`, `outcome` | `state`, `visit`, `result`, `raw` |
| Node `after`, ordinary transition actions | Those names plus `outcome` |
| Repeat-limit transition actions | Those names plus `limit` |

Allowed syntax includes strings, numbers, booleans and `None`; dictionaries,
lists and tuples; dictionary fields; indexing into dictionaries, lists, tuples
and strings; `and`, `or`, `not`; conditional expressions; comparisons
`== != < <= > >= in not in is is not`; arithmetic `+ - * /` and unary `+ -`;
and list or generator comprehensions with one simple loop variable and optional
filters. Dictionary attribute notation such as `state.output` only reads a
dictionary key. Missing names or keys produce an error; use `get` or a
conditional for optional data.

These are the complete permitted functions. Calls use positional arguments:

| Functions | Behavior |
| --- | --- |
| `len`, `str`, `bool`, `int`, `float` | Length and ordinary value conversions. |
| `all`, `any` | Aggregate truth values; support generator expressions. |
| `json(value)` | Serialize a value as indented JSON text, preserving Unicode. Useful for prompts and `write` actions. |
| `strip(value)` | Require a string and remove surrounding whitespace. |
| `text(value)` | Require a nonempty string after removing surrounding whitespace. |
| `is_string`, `is_bool`, `is_list`, `is_dict` | Type checks. |
| `get(mapping, key, default=None)` | Dictionary lookup with an optional default. Arguments are evaluated eagerly; guard a potentially missing default expression. |

Method calls, imports, arbitrary function calls, keyword arguments, slicing,
assignment, and nested comprehension loops are unsupported. Quote expressions
containing YAML-sensitive punctuation or boolean-looking literals.

## CLI, Python API, and shared state

```bash
python3 workflow_runner.py workflows/author_critic.yaml workflows/clean_up.yaml < statement.md
python3 workflow_runner.py workflows/clean_up.yaml < theorem-and-proof.md
python3 workflow_runner.py edit_note.yaml --model gpt-6-astra --set editing_rounds=3 < note.md
```

The CLI initializes `input`, `statement`, and `source` to the same nonempty UTF-8
standard input, with surrounding whitespace removed. It rejects NUL characters.
These are ordinary state keys after initialization. New workflows usually read
`state.input` and assign `state.output`.

All graphs in a chain are loaded and prepared before the first model call.
They then receive the same mutable state in order. A truthy `state.failed`
stops **between** graphs, not between individual nodes; a failing node should
also route to `end`. On success the CLI emits `workflow_result`, with `output`
equal to `state.output` or `""` if unset. It exits with 0 on success, 1 on errors
or a failed chain, and 130 on Ctrl-C. Other events are emitted by the model
transport and YAML actions. The UI's built-in graphs preserve their existing
`critic_result`, `final_result`, and failure events.

| CLI option | Meaning |
| --- | --- |
| `--model`, `--reasoning-effort` | Global model and effort fallback. |
| `--author-model`, `--critic-model`, `--writer-model` | Model override for those roles. |
| `--author-effort`, `--critic-effort`, `--writer-effort` | Effort override for those roles. |
| `--speed` | Global `fast` or `standard` generation speed; DeepSeek uses standard speed. |
| `--reasoning-summary` | Public activity summaries: `none`, `concise` (default), or `detailed`. |
| `--state-file` | Load initial state from a JSON object instead of reading standard input. It does not add the stdin aliases. |
| `--start-node` | Start the first workflow at the named node; later workflows start at their first node. |
| `--critic-rounds` | Consecutive repaired-round limit, from 1 to 100; default 2. Reaching it saves verification incomplete. |
| `--thinking-hours` | Shared elapsed-workflow limit covering author, critic, and final requests: greater than 0 and at most 168 hours, default 168. |
| `--elapsed-seconds` | Nonnegative elapsed time to count before starting the workflow, default 0. |
| `--author-limit-file` | Live JSON control file containing `{"hours": ...}` for the shared workflow time limit. |
| `--author-steer-file` | Live instruction queue delivered to the persistent author session. |
| `--author-prompt-file`, `--critic-prompt-file`, `--final-prompt-file` | Replace the corresponding named prompt with UTF-8 file contents. |
| `--set NAME=VALUE` | Set any named option; repeat as needed. JSON values are decoded, and other values remain strings. Applied after ordinary flags. |

Custom roles use the same option convention: for `role: editor`, set
`--set editor_model=gpt-6-astra` or `--set editor_effort=high`. Prompt overrides
can also be supplied through the `prompts` option, a dictionary of prompt names
to replacement strings.

```python
from pathlib import Path
import workflow_runner

state = workflow_runner.execute(
    Path("summarize.yaml"),
    {"input": "Text to summarize."},
    options={"model": "gpt-6-astra"},
)
print(state["output"])
```

`execute(path, state=None, *, options=None)` runs one graph and returns the same
mutable state; omitted state becomes `{}`. `execute_workflows(paths, state,
options=None)` runs a chain. `options={"start_node": "critic"}` can enter a
workflow at a saved checkpoint when the supplied state contains that node's
required inputs. Python callers provide all input keys they need;
the CLI aliases are not automatically added. These functions return state and
emit transport/YAML events, but only the CLI adds the terminal `workflow_result`
event. Options are shared across a chain and do not become expression-context
variables.

For compatibility, the `author_input` option can provide an already-expanded
initial assignment for goal nodes. It bypasses the main prompt's marker substitution
and marker-count check; lifecycle templates still apply. Ordinary custom
workflows should use the YAML `prompt`, `task`, and `marker` entries.
For UI continuation, `author_input_file` reads the existing initial-prompt file
verbatim, preserving its original whitespace and line endings. `goal_thread_id`
selects the saved conversation to resume. Neither option creates a memory file.

## Check a workflow offline

After saving the two complete examples above, this script checks their format,
prompt references, settings, response schemas, state forwarding, and a repeat
route without starting Codex:

```python
import json
from unittest.mock import patch
import workflow_runner as runner

for path in ("summarize.yaml", "edit_note.yaml"):
    runner.prepare(runner.load_workflow(path), {})

with patch.object(runner, "structured", return_value=(
    {"summary": "A brief summary."}, '{"summary":"A brief summary."}'
)):
    assert runner.execute("summarize.yaml", {"input": "Long notes."})["output"] == "A brief summary."

responses = [
    {"text": "A shorter note.", "decision": "revise"},
    {"text": "A short note.", "decision": "ready"},
    {"headline": "Update", "body": "A short note."},
]
with patch.object(runner, "structured", side_effect=[
    (response, json.dumps(response)) for response in responses
]) as calls:
    state = runner.execute("edit_note.yaml", {"input": "A long note."})
    assert calls.call_count == 3
    assert calls.call_args_list[1].args[0].endswith("NOTE: A shorter note.")
    assert state["output"] == "Update\n\nA short note."
```

Mocking `structured` skips the transport and its schema validation;
the YAML's `require` checks and graph actions still run. For a goal graph,
mock `goal_runtime.goal_session` with a generator yielding the documented goal result and
accepting `resume` dictionaries. The repository's regression suite uses these
boundaries to verify workflows without paid model calls:

```bash
python3 -m unittest discover -s tests -v
```
