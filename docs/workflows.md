# Writing workflows

`workflow_runner.py` executes a YAML graph. The YAML owns prompts, response
shapes, result checks, state updates, and routing. The runner supplies model
calls and persistent goal sessions. Node names carry no special meaning, so a
custom graph needs no Python handler.

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
second repaired round accepts the latest candidate without another audit.

## Reading the bundled workflows

In [`author_critic.yaml`](../workflows/author_critic.yaml), `author` uses `goal`.
Its `task` reads `state.statement`, and `marker` inserts it into the author
prompt. `outcome: result.outcome` selects `proof` or `failure`. The `merge`
action preserves the returned solution, memory, or summary; the next action
sets `failed` from that outcome. A proof goes to `critic`; failure ends the graph
and prevents the chained cleanup from starting. On rejection, `resume` supplies
the latest solution, `report.bugs`, and `round` to the existing author session.

The critic first saves `saved-candidate.md`, then its `parallel` block runs
three independent audits and checkpoints completed reports in `critic-audits.json`.
The coordinator consumes those actual reports. Its `response` describes exactly three checks, the verdict, repair
flag, complete solution, unresolved bugs, and historical metadata. Its
`require` expressions check the three reports and enforce consistent verdicts:
a clean pass has no failed checks or remaining bugs; a repaired pass has no
remaining bugs; rejection has unresolved bugs and does not claim a completed
repair. `outcome` classifies the response as `pass`, `fixed`, or `reject`.

The critic emits the response, records its memory update **before** replacing
`state.solution` so the reviewed and repaired candidates stay linked, then
stores the latest solution, full `report`, and `round`. `pass` copies the solution
to `output` and ends. `reject` returns to the author. `fixed` repeats the critic
until `repeat: 2` or the `critic_rounds` override is reached; the terminal
branch marks the candidate approved, sets `output`, and emits the round-limit
acceptance event. The node's `before` actions distinguish the first audit from
subsequent independent rounds.

In [`clean_up.yaml`](../workflows/clean_up.yaml), `latex_editor` chooses
`proof_input` when `state.solution` exists and `source_input` otherwise. The
former receives statement and solution separately and saves `final-input.json`
for exact recovery; the latter receives a combined theorem/proof source. Its response requests `latex`, and `require`
checks that it is a nonempty string. The actions set `output`, replace the
corresponding `solution` or `source` for subsequent graphs, and emit
`final_result` with the raw response and final document. `next: end` finishes
its single node.

| Bundled prompt name | Purpose |
| --- | --- |
| `author` | Full proof-search assignment containing the `[STATEMENT]` marker. |
| `critic` | Independent proof audit, repair, and verdict instructions. |
| `critic_memory` | Additional instructions for returning historical metadata to the durable ledger. |
| `critic_input` | Coordinator template joining the completed audits, critic instructions, statement, and current candidate. |
| `critic_audit` | Independent auditor instructions with its assigned focus. |
| `recovery` | Initial author prompt when a saved candidate is rejected before an author session exists. |
| `final` | Instructions for the LaTeX editor. |
| `proof_input` | Cleanup template with separate statement and accepted solution fields. |
| `source_input` | Cleanup template with a combined theorem/proof source field. |

The ten remaining author prompts implement the session lifecycle; their uses
and placeholders are listed under [goal nodes](#goal-nodes). They are bound by
default when `lifecycle` is omitted.

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

`goal` keeps one session per node across visits. Its first visit starts a task;
later visits resume the same session using feedback from shared state. Closing
the graph closes all its sessions. The transport retains the existing durable
proof-search protocol: a completed candidate yields `outcome: proof`,
`solution`, and a live `memory` object; an unsuccessful or timed-out search
yields `outcome: failure` and `output` containing its summary.

| Entry | Meaning and default |
| --- | --- |
| `task` | Required expression selecting nonempty task text from `state` and `visit`. |
| `marker` | Required nonempty literal appearing exactly once in the goal's main `prompt`; the runner replaces it with the task. |
| `lifecycle` | Optional bindings for the ten lifecycle prompts below. Omission binds each key to the same-named prompt. A list of all ten names is equivalent; a mapping allows different prompt names. |
| `resume` | Required expression mapping containing `solution`, `bugs`, and `round`, used when re-entering the node. Bind the latest candidate, unresolved feedback, and reviewer round. |
| `stages` | Optional mapping with exactly `initial`, `resume`, and `failure`. Defaults to `{initial: solve, resume: repair, failure: failure}`. |
| `recovery` | Optional `{when, prompt}` for a first activation entered with saved feedback. If `when` is true, the named template receives `original_prompt`, `statement`, and formatted `repair` from the lifecycle prompts and `resume` bindings. Later visits resume the existing session normally. |

The lifecycle protocol still needs all ten prompt entries even when `lifecycle`
is omitted. These are the available placeholders; plain-text entries are not
formatted themselves:

| Lifecycle key | Purpose | Available placeholders |
| --- | --- | --- |
| `goal` | Objective supplied to the persistent goal controller. | None; plain text. |
| `initial` | First model input. | `original_prompt`, `memory_instructions`, `memory_snapshot` |
| `memory` | Instructions describing the controller's durable history. | None; inserted into `initial` as `memory_instructions`. |
| `anchor` | Immutable assignment file. | `original_prompt`, `statement` |
| `reanchor` | Wrapper reinjected on continuation, compaction, or revision. | `original_prompt`, `statement`, `memory_snapshot`, `instruction` |
| `continuation` | Instruction when an attempt ends without a complete candidate. | None; inserted into `reanchor` as `instruction`. |
| `compaction` | Instruction after root-session context compaction. | None; inserted into `reanchor` as `instruction`. |
| `repair` | Feedback to the existing session after a rejection. | `critic_label`, `revision_number`, `statement_block`, `solution`, `bugs` |
| `failure` | Instructions for preserving unfinished work. | None; inserted into `failure_input` as `instructions`. |
| `failure_input` | Final summary request after a stop condition. | `instructions`, `reason`, `memory_snapshot` |

`original_prompt` is the main prompt after marker substitution; `statement` is
the task text. `memory_snapshot` is the bounded ledger snapshot. `critic_label`
names the feedback round, `revision_number` starts at 1, and `statement_block`
is empty on ordinary session resumption because the reanchor already includes
the task. The formatted `repair` becomes `reanchor`'s `instruction`.

Use `outcome: result.outcome` and `after: [{merge: result}]` to expose the
session's result to the graph, then explicitly set `state.failed` when failure
should stop a chain. The built-in author shows the complete contract in
[`author_critic.yaml`](../workflows/author_critic.yaml).

The goal time limit is controlled by options, not extra YAML node fields:
`thinking_hours`, `elapsed_seconds`, and `author_limit_file`. The
`author_steer_file` option supplies the UI's live instruction queue to an active
session. The time limit interrupts an active goal search; an ongoing structured
call can finish, and a goal resumed
after its deadline produces a failure summary. With multiple goal nodes in one
graph, each session has its own ledger directory under `goal-memory/`.

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
is `memory`, `merge`, `set`, `write`, then `emit`. Separate actions usually read more
clearly. The engine updates `state.outcome` after node `after` actions, so use
`outcome` directly inside those actions to refer to the current result.

### Durable memory action

`memory` does nothing if `state.memory` is absent or `None`. Otherwise it expects
the runner's live `AuthorMemory` object, normally supplied by a goal result.
It is not a general dictionary store.

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
metadata is tolerated. Status and history updates do not choose graph routes;
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
| `--critic-rounds` | Built-in critic limit, from 1 to 100; the YAML default is 2. |
| `--thinking-hours` | Goal search's elapsed-workflow limit: greater than 0 and at most 168 hours, default 168. |
| `--elapsed-seconds` | Nonnegative elapsed time to count before starting a goal session, default 0. |
| `--author-limit-file` | Live JSON control file containing `{"hours": ...}` for goal time limits. |
| `--author-steer-file` | Live instruction-queue file consumed by the persistent goal session. |
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
first input for goal nodes. It bypasses the main prompt's marker substitution
and marker-count check; lifecycle templates still apply. Ordinary custom
workflows should use the YAML `prompt`, `task`, and `marker` entries.

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
mock `author_session` with a generator yielding the documented goal result and
accepting `resume` dictionaries. The repository's regression suite uses these
boundaries to verify workflows without paid model calls:

```bash
python3 -m unittest discover -s tests -v
```
