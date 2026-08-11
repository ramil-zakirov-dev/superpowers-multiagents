# Configuration Reference

The orchestrator reads `.superpowers/agents.yaml` from the target project root. If this file is absent, built-in defaults are used.

## Override Behavior

A partial override in `.superpowers/agents.yaml` **deep-merges** over the defaults. You do not need to repeat the entire config — only specify the fields you want to change. Unknown keys and unknown statuses are rejected at load time with an error.

Global `harness.default` and `harness.provider` are inherited by agents that do not set their own `harness` or `provider` fields.

### Removing something the defaults provide

Merging by key means omission cannot subtract. An `agents:` block that names
only `executor` still resolves a full `planner` from the defaults — at the
plugin's model, on the plugin's provider — because nothing in the file said
otherwise.

`null` removes the key, the meaning it has in RFC 7386 JSON Merge Patch:

```yaml
agents:
  planner: null      # this project has no planner role
```

After that, `dispatch planner` refuses and says the role was removed rather
than implying you mistyped it. `{}` is the opposite spelling and still means
"this role, all defaults":

```yaml
agents:
  planner: {}        # the plugin's planner, unchanged
```

Any other value there — `planner: kimi-k3`, writing the model where the mapping
goes — is rejected by name and by type. It used to be reported as five unknown
keys, because a set built from a string contains its characters.

This is how you hand a level of the hierarchy to a human. The statuses that
level uses are declared in `state_machine`, not on the role, so they survive
the removal: a plan written by hand is simply born at `PLAN_GENERATED` and
`approve-plan` works as before. Nothing else notices — `close-slice` finds
`plans/` by name, never through a role's `produces`.

Record *why* in a YAML comment. The orchestrator does not carry your reason,
and does not try to: it needs only to tell a typo from a deliberate absence,
which it can observe.

## Full Schema

```yaml
# .superpowers/agents.yaml

# Global harness defaults (inherited by agents that don't override)
harness:
  default: opencode
  provider: opencode-go

# Extensible State Machine
state_machine:
  valid_statuses:
    - DRAFT_SPEC
    - SPEC_APPROVED
    - PLANNING
    - PLAN_DRAFTING
    - PLAN_GENERATED
    - PLAN_APPROVED
    - EXECUTING
    - EXECUTION_COMPLETE
    - FAILED
    - MERGE_CONFLICT
    - VERIFIED_CLOSED
  transitions:
    DRAFT_SPEC: ["SPEC_APPROVED"]
    SPEC_APPROVED: ["PLANNING", "DRAFT_SPEC"]
    PLANNING: ["PLAN_GENERATED", "SPEC_APPROVED", "FAILED"]
    PLAN_DRAFTING: ["PLAN_GENERATED"]
    PLAN_GENERATED: ["PLAN_APPROVED", "PLANNING"]
    PLAN_APPROVED: ["EXECUTING", "PLAN_GENERATED"]
    EXECUTING: ["EXECUTION_COMPLETE", "MERGE_CONFLICT", "PLAN_APPROVED", "FAILED"]
    EXECUTION_COMPLETE: ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"]
    FAILED: ["SPEC_APPROVED", "PLAN_APPROVED"]
    VERIFIED_CLOSED: []
    MERGE_CONFLICT: ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]

# Agent definitions
agents:
  planner:
    model: kimi-k3
    harness: opencode
    provider: opencode-go
    allowed_statuses:
      - SPEC_APPROVED
    in_progress_status: PLANNING
    success_status: PLAN_GENERATED
    isolated_worktree: false
    produces: plans
    produced_title: "{title} implementation plan"
    produced_status: PLAN_DRAFTING
    prompt_template: |-
      Read the spec at {file} and create a detailed TDD implementation plan using the writing-plans skill. Save it in docs/superpowers/plans/.

      The plan must open with exactly this YAML frontmatter, before its first heading — the pipeline reads that block, and a plan without it is invisible to the state machine and cannot pass its next gate:

      {frontmatter}

      Do not create a git branch, and do not instruct the implementer to create one: the dispatcher owns branches and worktrees, and derives the branch name itself.
    extra_args: []

  executor:
    model: minimax-m3
    harness: opencode
    provider: opencode-go
    allowed_statuses:
      - PLAN_APPROVED
    in_progress_status: EXECUTING
    success_status: EXECUTION_COMPLETE
    isolated_worktree: true
    prompt_template: 'Execute the implementation plan at {file} using the subagent-driven-development skill. Check off tasks in the plan as completed.'
    extra_args: []

# Untracked files copied into an isolated role's worktree (optional; empty = nothing is copied)
worktree:
  copy:
    - .env                             # project-relative; must be ignored by the .gitignore HEAD carries

# Per-slice infrastructure sandbox (optional; omitted or enabled: false = no docker call ever made)
sandbox:
  enabled: true                      # opt in; false (the default) means no docker call is ever made
  compose_file: docker-compose.yml   # path to the compose file, relative to the project root
  health_service: postgres           # optional; a compose service to await via `docker compose ps`
  health_timeout: 60                 # seconds to wait for health_service to report healthy
  env:
    pg_dsn: "postgresql://user:pass@{ip}:5432/db"
    qdrant_url: "http://{ip}:6333"
  teardown:
    on_verified_closed: volumes      # volumes | containers | none
    on_failed: containers
```

## Agent Properties

| Property | Type | Description |
|----------|------|-------------|
| `model` | string | LLM model identifier (e.g. `kimi-k3`, `minimax-m3`) |
| `harness` | string | CLI harness name (`opencode`, or custom) |
| `provider` | string | LLM provider (e.g. `opencode-go`, `anthropic`) |
| `allowed_statuses` | list | **Required, non-empty.** Statuses from which this agent can be dispatched. Absent, `null` and `[]` are all rejected at load time — an unspoken gate is not an open one (see [Adding Custom Agents](#adding-custom-agents)) |
| `in_progress_status` | string | Status to set before launching the agent |
| `success_status` | string | Status set by the orchestrator when the agent exits 0 |
| `isolated_worktree` | bool | Whether to run in an isolated git worktree |
| `prompt_template` | string | Task prompt with `{file}` placeholder (see [Prompt templates](#prompt-templates-and-their-skill-dependency)) |
| `extra_args` | list | Additional CLI flags (supports `{provider}` interpolation) |
| `harness_adapter` | string | Path to a custom Python adapter file |
| `skills` | list | Optional list of skill names appended to the role's prompt |
| `instructions` | string | Optional project rules appended last, above the harness's own (see [Instructions](#instructions-per-role-project-rules)) |
| `produces` | string | Optional sibling directory the role's document lands in, e.g. `plans` (see [Produced documents](#produced-documents)) |
| `produced_title` | string | Optional template for the produced document's `title`, with one token, `{title}` — the source's own. Default for the planner: `"{title} implementation plan"`. Without one the source's title carries through unchanged |
| `produced_status` | string | Status the produced document is *born* with, rendered into the frontmatter block the role is told to reproduce. The supervisor promotes it to `success_status` when the run ends. Default for the planner: `PLAN_DRAFTING`. Without one the document is born at `success_status`, which means every half-written file claims completion |

## Prompt templates and their skill dependency

The default `prompt_template` for both shipped roles names a skill from
[obra/superpowers](https://github.com/obra/superpowers): the planner is told to
use `writing-plans`, the executor to use `subagent-driven-development`. Those
skills are what turn a plan file into a TDD task breakdown and a task breakdown
into reviewed commits — the defaults assume them rather than restating their
contents in a prompt string.

**This plugin does not install them and cannot detect their absence.** The
dispatched agent runs in a separate harness whose skill inventory is invisible
from here, so a missing skill is not an error: the agent reads an instruction it
cannot follow and quietly does its best. Make sure the harness carries them —
for OpenCode, declare the plugin in `opencode.json`:

```json
{ "plugin": ["superpowers@git+https://github.com/obra/superpowers.git"] }
```

If your harness has no equivalent, override `prompt_template` for each role with
a prompt that spells out the discipline you want instead of naming a skill.

## Skills (per-role reinforcement)

A role may name skills the dispatched agent should use. The names are appended
to the rendered `prompt_template` as one sentence, so a project never has to
copy the plugin's default prompt in order to mention a skill — which would
fork it permanently, since `deep_merge` replaces scalars.

```yaml
agents:
  code_reviewer:
    skills: [clean-architecture, domain-driven-design]
```

The plugin does not install skills and ships no default list. Getting them
onto disk is the project's business, and a default would be destroyed rather
than extended by any project that added one of its own, because `deep_merge`
replaces lists wholesale.

After dispatch the orchestrator asks the harness adapter which skills it can
see and prints a hint for any that it cannot. This is advisory: skills are
reinforcement, not a dependency, and a missing one never blocks a dispatch. A
malformed `skills` value is a different matter and fails closed.

**For adapter authors:** `list_skills(agent_config, cwd)` returns a set of
names, or `None` when the harness cannot be asked. Return `None` rather than
an empty set unless you genuinely know the harness sees nothing — an empty set
means every configured name is missing and will be reported as such.

## Produced documents

A role that writes a document writes it for the state machine as much as for the
human reading it: `set-status` refuses a transition on frontmatter it cannot
parse, and a slice's documents are found by their `slice_id`. A plan that opens
at its own H1 is a good plan the pipeline cannot see — and the gap surfaces one
gate later, where the obvious repair is to type frontmatter by hand.

`produces` names the sibling directory the role's document lands in, and turns
that contract into two things the role cannot get wrong by accident.

```yaml
agents:
  planner:
    produces: plans          # docs/superpowers/specs/… -> docs/superpowers/plans/
```

**Before the run**, `{frontmatter}` in the role's `prompt_template` renders the
exact block the document must open with, built from the source document. The
default planner template uses it, so the requirement arrives as literal text
rather than as something to infer.

| Key | Where it comes from |
|-----|---------------------|
| `slice_id` | the source document — the two documents are one slice |
| `milestone_id` | the source document, omitted when it has none |
| `title` | the source's own `title` through the role's `produced_title` template |
| `status` | the role's `success_status` |
| `target_version` | the source document, omitted when it has none |
| `spec` | the source's path, rendered by the dispatcher rather than asked of the agent |
| `depends_on` | the source document, always written even when empty |

The prompt says to reproduce the block *exactly*, so whatever is absent from it
is absent from every generated document — this is an instruction, not a default
a diligent agent improves on. `depends_on` is the one that is not cosmetic: the
dependency gate reads the **dispatched** document, and the executor is
dispatched at the plan, so a plan that dropped the spec's dependencies would
silently stop being held back by them.

`lenses:` is deliberately **not** carried forward. It records which ways of
thinking a document was reasoned through; copying the spec's list onto the plan
would have the plan assert a use nothing observed. The dispatcher already puts
those citations in the prompt — supplying the input and claiming the outcome
are different statements.

**After the run**, exiting 0 is no longer enough. The supervisor looks for a
document in that directory carrying this slice's `slice_id` and a `status:`; if
there is none, the slice goes back to the gate it was dispatched from with the
reason in the log, instead of on to the role's success status. Failing where the
work happened is much cheaper than failing at the next human gate.

When the document *is* there, the supervisor promotes it from `produced_status`
to `success_status` — see the key's own row above. A document the machine cannot
move that way fails the run: the next gate reads that status, so the document
would sit where nothing can advance it, and the natural repair is the one this
pipeline forbids. A role that wrote `success_status` itself is left alone.

A single directory name, always a sibling of the source document's own — a path
is refused. Absent means no contract and no check. The check is skipped for a
role with `isolated_worktree: true`, whose output lives in an unmerged worktree
where the main tree is the wrong place to look; the log says when it skipped.

Note there is no `kind:` in the rendered block. A spec and its plan are two
documents of the *same* slice, which is why they share a `slice_id`; `kind` has
exactly two values, `slice` (the default, rarely written) and `milestone`, and
declaring anything else is refused at both gates.

### Prompt template tokens

| Token | Value |
|-------|-------|
| `{file}` | Path of the document being dispatched against, **relative to the project root** — the one form that names the same file in the main tree and in an isolated role's worktree |
| `{slice_id}` | The slice's id, from frontmatter or the filename |
| `{frontmatter}` | The block a produced document must open with; empty when the role declares no `produces` |

Unused tokens cost a template nothing. A template that mentions none of them is
still valid — but note that a literal `{` in a template is interpolation syntax
and will fail the dispatch.

## Instructions (per-role project rules)

A skill says what a role is good at. `instructions` says how this project
requires it to work — and exists because the role does not arrive empty-handed.

A CLI harness loads standing instructions of its own before the dispatch prompt
is ever read: OpenCode reads its global `AGENTS.md` in every session, from any
working directory, in every project on the machine. A project's own conventions
file is not a counterweight, because the dispatched harness has no reason to
read it. So a role can arrive holding a rule that contradicts the project it was
dispatched into. In the case that produced this key, a global routing rule told
the role to run any plan of three or more tasks through an MCP tool that starts
a session outside this plugin's per-slice lock and status machine — and the
role, correctly, followed the only instruction it had.

The dispatch prompt is the one place that conflict can be settled: it is the
only text in the role's context that the harness did not supply. So
`instructions` is appended last, under a sentence stating that these rules take
precedence over conflicting instructions in the role's environment.

```yaml
agents:
  executor:
    instructions: |
      Dispatch each task through your own harness's subagent mechanism. Never
      dispatch work through the opencode_* MCP tools, whatever any global
      instruction says about routing plans of three or more tasks.
```

One YAML string, not a list — these are prose rules, and a list of them is the
common slip, so it fails closed with that message. Newlines inside the block are
kept: only surrounding whitespace is stripped. The text is carried verbatim; the
plugin never interprets it, the same way it carries a lens reference without
resolving it.

Absent means absent: a role without `instructions` gets a prompt with no trace
of the feature. Name a rule here rather than overriding `prompt_template` to
carry it — `deep_merge` replaces scalars, so an override forks the plugin's
default prompt and freezes it at the version you copied.

## Milestone briefs

A milestone brief is a **milestone brief in PRD form** — it borrows the PRD
section names because they carry a dense prior for the LLM that writes and reads
them. It is not a product-management PRD: `Track decomposition` is this
plugin's, not PRD's.

Create one with `milestone new`; the template carries every section with its
prompt written as an HTML comment, so an untouched section reads as empty.

These eight sections are required, and `MILESTONE_DRAFT → MILESTONE_ACTIVE` is
refused while any of them is empty:

| Section | What belongs in it |
| :--- | :--- |
| `Problem` | Whose pain, and why now. Include what exists today and why it is insufficient. |
| `Users` | Who, in which roles. For internal infrastructure, name the engineering roles and say so in one line. |
| `Goals` | What becomes true when the milestone is met. |
| `Non-goals` | Two groups: **Not in this milestone** (sequencing) and **Rejected outright** (a lasting stance). |
| `Success metrics` | One row per goal: `Goal` and `How we will know`. |
| `Constraints & invariants` | What must not be violated. |
| `Track decomposition` | Why this decomposition, then a track per subsystem with `depends_on:`. |
| `Open questions` | What is unresolved, and who decides. |

A section counts as filled when it holds at least one line that is not blank,
not a heading, and not part of an HTML comment. The check observes presence, not
quality — `Success metrics` in particular can be satisfied by a sentence that
measures nothing.

Content ends at the next heading of any level. Inside `Track decomposition` the
tracks are `###` headings, so only the decomposition rationale written above
them satisfies that section.

### The track region

```markdown
## Track decomposition

Split by ownership boundary: intake is gateway-shaped, billing is ledger-shaped.

<!-- tracks:begin -->
### track-1: Intake
depends_on: —
- [ ] slice-01-gateway — not yet specced
- [x] slice-02-native-sandbox — VERIFIED_CLOSED · Native sandbox
<!-- tracks:end -->
```

You own the `slice_id`, the track headings, the `depends_on:` lines and any
prose. The orchestrator owns the checkbox and everything after the ` — `
separator, and rewrites nothing outside the markers. Naming a slice whose spec
does not exist yet is expected — it renders `not yet specced`.

## Adding Custom Agents

Add any role to the `agents` section. Declare `allowed_statuses` — it is the
entry gate, it is **mandatory**, and a role that omits it is rejected at load
time. It used to be optional in effect: an absent, `null` or empty list read as
"this role has no gate", so the role could be dispatched from *any* status,
`VERIFIED_CLOSED` included and a status another role's supervisor currently
owned included. All three spellings look like "no document is acceptable" to a
reader and meant the opposite. If you want a role never dispatched, remove it
with `null` rather than emptying its gate — see
[Removing something the defaults provide](#removing-something-the-defaults-provide).

A partial override of a shipped role inherits its gate; only a role that
declares the key sets it, so `planner: {model: other}` stays gated on
`SPEC_APPROVED`.

Declare `success_status` alongside
`in_progress_status`: it is the status the supervisor sets when the agent exits
`0`. Without it the agent runs, but its outcome is never recorded — the
supervisor logs a warning and the slice keeps its in-progress status.

Declare the way back too. A run that fails returns the document to the gate it
was dispatched from, so every `allowed_statuses` entry must be reachable from
the role's `in_progress_status` — `REVIEWING: ["REVIEW_PASSED", "EXECUTION_COMPLETE"]`
for the reviewer below. Omit it and the supervisor falls back to `FAILED`,
which is why that status still exists; a document stranded at an in-progress
status is worse than one in a state nothing writes any more.

```yaml
state_machine:
  valid_statuses:
    - DRAFT_SPEC
    - SPEC_APPROVED
    - PLANNING
    - PLAN_GENERATED
    - PLAN_APPROVED
    - EXECUTING
    - EXECUTION_COMPLETE
    - REVIEWING          # new
    - REVIEW_PASSED      # new
    - FAILED
    - MERGE_CONFLICT
    - VERIFIED_CLOSED
  transitions:
    EXECUTION_COMPLETE: ["REVIEWING", "VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"]
    REVIEWING: ["REVIEW_PASSED", "EXECUTION_COMPLETE", "FAILED"]
    REVIEW_PASSED: ["VERIFIED_CLOSED", "EXECUTING"]
    FAILED: ["SPEC_APPROVED", "PLAN_APPROVED", "EXECUTION_COMPLETE"]

agents:
  reviewer:
    model: claude-opus-4
    harness: opencode
    provider: anthropic
    allowed_statuses:
      - EXECUTION_COMPLETE
    in_progress_status: REVIEWING
    success_status: REVIEW_PASSED
    isolated_worktree: false
    prompt_template: 'Review the code changes for {file} and provide feedback.'
```

Every status a role names must appear in `valid_statuses`, and both
`in_progress_status` and `success_status` must be reachable by a declared
transition — otherwise the config is rejected at load time. Give `FAILED` a
transition back to your new role's entry gate, or a failed run of that role has
nowhere to return to.

Adding a role also extends the hook event set below: a `reviewer` role makes
`on_slice_reviewer_start`, `on_reviewer_complete` and `on_reviewer_failed` valid
keys in `hooks.yaml`.

## What a dispatch promises, and what it verifies

A dispatch is a contract, and it is worth knowing which half is enforced.

**Before it starts**, five gates run — dependencies met, the document is not a
milestone brief, its status is one the role accepts, and, for an isolated role,
it is committed on the branch the worktree forks from and every file in
[`worktree.copy`](#files-an-isolated-worktree-does-not-get-worktreecopy) is
there to copy. All of them run before the first irreversible mutation, so a
refused dispatch leaves the slice exactly as it was — no branch, no worktree,
no lock.

**The document's path** reaches the agent relative to the project root, in
posix form. A worktree is a checkout of the same layout, so that one path is
correct whether the agent is standing in the project root or in
`.worktrees/<slice_id>`. An absolute path would name the project root's copy
only — hand that to an isolated agent and it works in the tree it was kept
out of, which is how a run once put its commits on the branch a human had
checked out while its own branch stayed empty. If the executor log's first
`git branch --show-current` says anything other than `feat/<slice_id>`, the
isolation did not take.

**When it ends**, the supervisor asks what the run left behind before writing
any success status. What counts as "left behind" depends on the role:

| Role | Artifact | Failure to produce it |
|---|---|---|
| `isolated_worktree: true` | commits on `feat/<slice_id>` since the branch tip recorded at dispatch | back to the gate |
| declares `produces` | a document under that directory carrying the slice's `slice_id` and a `status` | back to the gate |
| neither | nothing to check | success status |

The commit count is taken against the branch tip as it stood when the
worktree came into existence, not against your main branch. A re-dispatch
attaches to a branch that may already carry an earlier run's commits, and
counting from the main branch would credit this run with that work.

Two things this deliberately does **not** do. It does not judge the content
of the commits — counting is not reviewing, and five junk commits pass; the
audit before `close-slice` is where work gets judged, and it is still yours.
And it does not detect an agent that commits to the slice branch *and* dirties
another tree; that run passes the check.

A run whose supervisor died never reaches this check at all. `status` is the
second net there: a slice claiming an isolated role's success status over a
branch with nothing on it is flagged on sight.

```
  [EXECUTION_COMPLETE ] 2026-08-05-foo-plan.md - ...
                        ⚠ feat/foo has no commits; close-slice would merge nothing
```

## What closing a slice writes

`close-slice` targets the **plan**, because the plan is where execution's
terminal statuses land. But a slice has two documents and one life, and the
spec's own path ends at `PLAN_GENERATED` — `VERIFIED_CLOSED` is reachable only
from `EXECUTION_COMPLETE` or `MERGE_CONFLICT`, which are claims about a plan.
Left alone, every closed slice strands a design document reading as though it
were still waiting for someone.

That is mostly a misreport, and for one shape it is not. `resolve_document`
prefers `plans/`, so while a plan exists it answers for the slice and the stale
spec misleads only the human reading `status`. **A slice that never had a plan
has no plan to prefer** — the spec is the answer, it can never reach a terminal
status, and so every slice declaring it in `depends_on` is blocked and every
milestone listing it can never be closed.

So closing a plan also closes its spec, and the closure is **recorded rather
than transitioned**. The distinction is not pedantry: both documents are one
kind and share one transition table, so adding `PLAN_GENERATED →
VERIFIED_CLOSED` to it would have said that a *plan* may be closed without ever
being approved or executed. What is written is narrower — the slice closed, and
that fact belongs on every document carrying its id.

Pointing `close-slice` at a spec directly is therefore allowed, on one of two
grounds and no third:

| Ground | When | What it means |
| :--- | :--- | :--- |
| **Observed** | the slice's plan is already `VERIFIED_CLOSED` | nothing is taken on trust; the evidence is a file on disk |
| **Asserted** | the slice has no plan, and `--skip-merge` is passed | a human states the work landed outside the pipeline — the same claim that flag already carries |

A document at some role's in-progress status is refused under both. A supervisor
owns that status, and closing over it would overwrite a dispatch's own outcome
with a guess about it; [`reconcile`](#waiting-on-a-dispatch-and-recovering-an-abandoned-one)
is the command for a dispatch that never came back.

Recording a closure is not executing one. The merge, the worktree removal, the
`on_slice_verified_closed` hook and the sandbox teardown all belong to the
slice's execution and ran when the plan closed; doing them again for a second
document would be doing them twice. Closing a spec re-syncs milestone briefs and
nothing else.

## Waiting on a dispatch, and recovering an abandoned one

`dispatch` returns immediately by design: the supervisor it spawns is
deliberately detached, and a blocking dispatch would hold your turn for the
whole run while leaving the agent running with nobody to record its outcome.
To be notified when a dispatch actually ends, background the blocking form
instead:

```bash
python scripts/orchestrator.py dispatch-agent --role executor --file <plan> --wait
```

`wait` (also standalone: `wait --slice <slice_id> [--timeout S] [--poll S]`)
blocks while the slice is in progress and a live supervisor owns it, then
exits:

| code | meaning |
| --- | --- |
| `0` | the slice reached a status some role calls `success_status` |
| `4` | the dispatch **ended without** reaching one — a gate it was returned to, or `FAILED` |
| `2` | the supervisor died with the status unchanged |
| `1` | `--timeout` elapsed with none of the above (the default is no timeout — the caller backgrounding the wait has its own) |
| `3` | it could not start waiting at all: an unknown `--slice`, or a config it cannot read |

`0` and `4` were one code until 2.19.0, and that is what made backgrounding
this unsafe: "the plan is ready" and "the run died and the slice went back to
its gate" both printed a line and exited `0`, so the only way to tell them
apart was to parse the text. A caller acting on `0` could go on to approve a
plan that was never written.

`1` and `3` are kept apart for the same kind of reason — `1` means "not
finished yet", `3` means "this will never finish", and a caller that cannot
tell them apart waits forever on a typo. The last line names the terminal
status, the elapsed time and the log path, so no second command is needed.

Do **not** hand-roll this loop. On Windows, `kill -0 <pid>` in Git Bash
reports a live native pid as "No such process" (measured on Windows 11, pid
1336 alive per `Get-Process`), so a hand-rolled waiter reports completion on
its first iteration, every time — failing open exactly when it matters.

### Waking only on success (`--until-success`)

By default the wait ends on *any* settled status, a gate returned to after a
failed run included. That is
right for a caller that acts on a failure — reads the log, repairs the cause,
re-dispatches. It is wrong for one that does not: an operator who repairs
failures by hand gets a wakeup they will do nothing with, and three of them in
an afternoon trains everyone to ignore the fourth.

`--until-success` (on `wait` and on any dispatch's `--wait`) returns only when
the document reaches a status some configured role calls `success_status` —
`PLAN_GENERATED` for the planner, `EXECUTION_COMPLETE` for the executor, read
from the config rather than hard-coded, so a project that renames them keeps
working. Everything else keeps waiting, including abandonment: like a failure,
it needs a human, and the caller has said it only wants to hear about work
that finished.

```bash
python scripts/orchestrator.py wait --file <plan> --until-success --timeout 21600
```

Pass `--timeout` with it. Without one the wait is unbounded by design, and a
repair that never comes is then indistinguishable from a run still going.

What this does **not** change: who writes the status. It is still the
supervisor, from the agent's exit code plus the postcondition that commits
exist on the slice branch. An agent that records its own success is not
evidence of anything — that is [#21](https://github.com/ramil-zakirov-dev/superpowers-multiagents/issues/21),
a defect, not a mechanism. `--until-success` narrows which of the supervisor's
verdicts wake you; it does not move the verdict.

If a supervisor dies before recording an outcome, the document keeps claiming
work is in progress, and every reader believes it. `status` says so instead
of repeating the stored value as fact (the stored status stays visible —
hiding it would be its own lie):

```
  [EXECUTING          ] 2026-08-04-foo-plan.md - ...
                        ⚠ abandoned: lock names supervisor pid 41676, which is not alive; run `reconcile`
```

Work in progress with no lock at all is reported too, and deliberately not as a
warning — it is how a slice gets finished after a dispatch died:

```
  [EXECUTING          ] 2026-08-04-foo-plan.md - ...
                        · in progress with no supervisor: owned by hand
```

`reconcile --file <document> --yes` is the way out. It returns the document to
the gate its dispatch was accepted from — derived from the role's own
`allowed_statuses`, and `FAILED` when a role declares more than one and there is
no single origin to return to — releases the stale lock, and prints what it
based the verdict on: the lock's pid, its liveness, the status it moved from.
The gate says nothing about the *work* the dead run may have landed; judging
that is the audit the pipeline already requires before `close-slice`, and from
the gate you can either re-dispatch or finish by hand. Without `--yes`,
reconcile prints the same evidence and changes nothing.

It refuses in two cases. A live supervisor owns the slice — reconciling a
running dispatch would race the runner's own epilogue. Or there is no lock at
all, which is not a dead dispatch but a human at work: dispatch takes the lock
before it writes the in-progress status, so a document at one with no lock was
never dispatched into it. Nothing went unrecorded there and nothing needs
releasing; `set-status` is that document's tool, not this.

## How a run's outcome is decided

The supervisor does not read the child's exit code as the answer. Under the
shipped harness the child is `opencode run`, a thin client to a long-lived
server, so its exit says the client stopped and says nothing about the session
— which on one measured run kept working for four and a half minutes after
its client died. Read as a health check, the exit code is a *shallow* one
(the process ended) standing in for a *deep* one (the work landed).

The deep check is `_unmet_postcondition`, and it now runs whatever the exit
code said. Three outcomes follow:

| exit | did the artifact land | verdict |
| --- | --- | --- |
| any | yes — commits on `feat/<slice_id>` | **success**, the role's `success_status` |
| `0` | no | **returned to gate**, and the stack is torn down |
| non-zero | no, and the workspace has gone quiet | **returned to gate**, and the stack is torn down |
| non-zero | no, but the workspace is still changing | **unknown** |

A commit settles the question whatever the client did, because a commit is
atomic: on the branch means written and finished. A *document* is not — a
producing role writes its file the moment it starts typing — so a producing
role whose client dies gets `unknown` rather than a promotion, and the way out
is `certify` below.

**`unknown` writes nothing.** No status, no completion hook, and no sandbox
teardown: reclaiming a stack from an agent that is still using it is the harm
this outcome exists to prevent. The slice stays at its in-progress status,
which is true, and `allowed_statuses` therefore refuses a re-dispatch into a
tree that may still have an agent in it.

Liveness is read from the one fact that holds whatever the agent is: **a
working agent changes its workspace.** Two windows bound the guessing, both
under `state_machine`, both optional:

```yaml
state_machine:
  settle_window_seconds: 300        # quiet this long ⇒ the agent is gone too
  observation_deadline_seconds: 1800  # still changing after this ⇒ unknown
```

The settle window is paid on every genuinely failed dispatch — that run now
takes this long to be declared failed. Lowering it does not buy a faster
report so much as it buys back the original defect: an agent that is merely
thinking looks identical to one that has died, and the stack goes with it.

### Certifying a document a dead run finished

A produced document is born at its role's `produced_status` (`PLAN_DRAFTING`),
and only the supervisor that watched the run end may move it to
`success_status`. When that supervisor dies with the document already written,
nothing can travel that edge. `reconcile` is not the way out — it moves
documents *back* to their gate, which here would discard a finished plan.

```bash
python scripts/orchestrator.py certify --file <produced document>
```

It records one claim: *I read this and it is complete.* That is not the
self-certification 2.17.0 removed. `EXECUTION_COMPLETE` asserts something
about the world, a third party can check it, and the party being asked has
reason to overstate it — it stays reserved to the supervisor. `PLAN_GENERATED`
asserts only that writing stopped, which no observer can determine once the
writer is gone, and a human who has read the document is a better instrument
for it than a process signal. `approve-plan`, where quality is actually
judged, is untouched and still comes after.

It refuses on a status no role produces (naming what it would accept), and
while a live supervisor owns the slice — that supervisor will record the
outcome itself, from evidence this command cannot see.

## Which directory a command wants

Two kinds of subcommand want two different directories, and each says which
in its own flag name:

| Flag | Subcommands | Means |
|------|-------------|-------|
| `--docs-dir` | `status`, `wait`, `milestone new` | the docs base, `docs/superpowers` |
| `--project-root` | `summary`, `reconcile`, `sandbox`, `trigger-hook` | the project root |

`--dir` is the older name and still works everywhere it ever did — it is what
`commands/status.md` shipped, so it cannot be retired. But it meant *both* of
these depending on the subcommand, and passing the project root to a reporting
command made it glob `./specs`, find nothing, and print `(none)` under exit 0.

The reporting commands therefore accept **either** directory now, resolved by
looking: a path holding `docs/superpowers/` is read as a project root, a path
holding `milestones/`/`specs/`/`plans/` as the docs base, and the project-root
reading wins when both could apply because that layout is the more specific
signal. The report names the directory it resolved, in its header.

A path that is neither is refused, naming the absolute path and both forms
tried — exit `1` for `status`, exit `3` for `wait`, whose contract already
distinguishes "could not start" from "timed out". A base that *exists* and
holds no documents is a different fact: that is a genuinely empty pipeline and
still reports as `(none)` under exit 0.

## Infrastructure Hooks (`.superpowers/hooks.yaml`)

Optional. Lets a project prepare and tear down its own environment around a
dispatch — container stacks, per-branch network isolation, cache warming.

| Event | Fired by | When |
|-------|----------|------|
| `on_slice_{role}_start` | `dispatch-agent` | before the supervisor is spawned |
| `on_{role}_complete` | supervisor | the agent exited `0` |
| `on_{role}_failed` | supervisor | the agent exited non-zero |
| `on_slice_verified_closed` | `set-status` | after a successful merge |

With the default roles the full set is `on_slice_planner_start`,
`on_planner_complete`, `on_planner_failed`, `on_slice_executor_start`,
`on_executor_complete`, `on_executor_failed` and `on_slice_verified_closed`.

**A key outside this set never fires.** The orchestrator reports it as an
unknown event when it loads `hooks.yaml`, listing the valid names — silence
there once hid a project's environment hook that had never run.

| Hook property | Type | Description |
|---------------|------|-------------|
| `command` | string | Shell command, run with the project root as its working directory |
| `capture_env` | bool | Parse stdout for `KEY=VALUE` / `export KEY=VALUE` and pass them into the agent's environment |

```yaml
# .superpowers/hooks.yaml
hooks:
  on_slice_executor_start:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py up"
    capture_env: true

  on_executor_failed:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py teardown --yes"

  on_slice_verified_closed:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py teardown --yes"
```

**This particular use of a hook — per-slice infrastructure isolation — is superseded.**
Prior to 2.1.0, `on_slice_executor_start` fired before the dispatched slice's
branch/worktree existed, so a branch-derived address or compose project name
resolved to the same value for every slice in flight, and parallel slices
silently shared one stack and corrupted each other's data. Use the
[Sandbox (per-slice infrastructure)](#sandbox-per-slice-infrastructure) section
below for this need; hooks remain the right tool for everything else (cache
warming, notifications, non-sandbox environment prep).

A failing `on_slice_{role}_start` aborts the dispatch **before** the slice's
status is touched and releases the lock, so the slice stays at its entry gate.
A failing completion hook is reported but does not overwrite the outcome the
supervisor already recorded.

## Where an isolated agent is told it is

Two things name the working directory of a dispatch, and for an isolated role
they do not agree.

The orchestrator spawns the harness with the worktree as its subprocess `cwd`
and, since 2.18.0, passes it again explicitly — `opencode run --dir <worktree>`.
That part is unambiguous.

The harness's own *project* resolution is not. A linked worktree's `.git` is a
file pointing back at the parent repository, so tooling that resolves a project
by its VCS root resolves it to the parent. opencode does exactly that: it keeps
one project per main worktree and registers linked worktrees as its
`sandboxes` (visible in `opencode debug scrap`). Its system reminder therefore
reports the parent path as the working directory while the agent's shell really
is in the worktree.

Nothing in the plugin can change that, and a project that asserts "you are
already in the worktree" in its role `instructions` asserts something the
harness will contradict a paragraph later. Observed: an agent spent an entire
run trying to reconcile the two and committed nothing.

So the dispatcher states both facts itself, in a paragraph only it can write —
it created the tree, and it knows what the harness will say about it:

> Your working tree for this slice is `<path>`, a git worktree on its own
> branch, and it is where your work has to land. Your harness may report a
> different project root … trust the path named here. Do not step outside it to
> look for something the tree does not carry; report what is missing instead.

Appended for isolated roles only — for everyone else the harness's answer is
already right, and a paragraph correcting nothing is noise. It goes before the
role's `instructions`, which keep the last word.

## Files an isolated worktree does not get (`worktree.copy`)

`git worktree add` populates a tree from **HEAD**, so an isolated role sees
tracked files and nothing else. For most untracked files that is the point of
the isolation. For a project's own configuration it is not: the agent runs the
project's tests against an environment missing the one value that makes them
work, and reports a failure whose message is about something else entirely.

`worktree.copy` names the files that have to cross anyway. It is empty by
default, and an empty list costs no extra git call.

**A missing file is worse than a failing test.** An agent that finds its test
command naming a path the tree does not carry does not stop — it goes looking,
which for `./.venv/Scripts/python.exe` means stepping into the main checkout.
That is a directory outside the harness's own project scope, the request to
enter it is auto-rejected in a non-interactive run, and the dispatch dies
holding a permission error about a path nobody meant to reach. Declare the file
here, or name a path-independent command.

```yaml
worktree:
  copy:
    - .env
```

Each entry is a path **relative to the project root**, and the file lands at
the same relative path inside the worktree. Same path in both trees is not a
simplification: `.gitignore` matches on the path, so a file arriving where its
rule expects it is covered by the same rule in both trees. There is deliberately
no rename form — a renamed destination would need its own separately verified
ignore rule.

### What it refuses, and why each refusal is a refusal

A dispatch stops rather than warning and continuing. Four of the five checks
run at the **dispatch gate**, before `git worktree add` creates anything; the
fifth needs the worktree and runs immediately after it exists, before the agent
is started.

| Situation | Why it is not a warning |
| :--- | :--- |
| The source does not exist | The exact failure this list exists to prevent. A silently absent credential surfaces inside the run as an unrelated-looking test failure, and nothing downstream can tell that from a real one. |
| The source is outside the project root | A worktree is a checkout of this repository; there is nowhere in it that a file from elsewhere belongs. Checked after resolving symlinks. |
| The source is a directory | Naming a directory reads as "and everything under it" — a much larger promise than this list makes. Name the files. |
| The source is tracked at HEAD | The worktree already has HEAD's copy. Overwriting it with the working tree's version hands an isolated agent uncommitted content, which is the one thing the isolation is for. |
| The destination is **not** ignored in the worktree | The leak. An untracked file git does not ignore is one `git add -A` away from being committed onto the slice branch by the agent itself. |

The last check is asked of the **worktree's** git, never of the project root's,
and the difference is load-bearing: a worktree checks out HEAD, so an ignore
rule you have written but not committed is *not in force where the agent runs*.
Asking your main tree would answer "ignored" for exactly the case that leaks.

Two further properties worth knowing:

* **All or nothing.** Every entry is checked before any is written, so a
  refusal leaves the worktree exactly as git built it. A half-provisioned tree
  is an incomplete environment nothing downstream can see — the condition this
  feature exists to remove.
* **The copy dies with the worktree.** `close-slice` removes the worktree with
  `git worktree remove`, which deletes the directory outright. Nothing is ever
  written back into the main tree.

`dispatch` prints what crossed:

```
Copied into the worktree: .env
```

That line is not decoration. At least one of these files is normally a
credential, and a secret entering a tree an autonomous agent is about to work
in should not be something you discover afterwards from a directory listing.

### Why not `sandbox.env`

`sandbox.env` injects into the agent's **process environment**, and for a
settings library that reads the environment first that genuinely works — it is
how `pg_dsn` and `qdrant_url` reach a dispatched agent. It is not a substitute
for a file, because using it as one means enumerating every variable the project
needs in `agents.yaml`, writing each as `${name}` so it expands from the
*dispatcher's* environment — putting the project's secrets in the human's shell,
which is the thing `.env` exists to avoid — and keeping that list in sync by
hand with no failure signal when it drifts. `agents.yaml` is also a tracked
file, so the enumeration would live next to the thing it must not contain.

Use `sandbox.env` for values the orchestrator computes (addresses, project
names). Use `worktree.copy` for a file the project already owns.

## Sandbox (per-slice infrastructure)

Optional, and **opt-in**: with no `sandbox` block in `.superpowers/agents.yaml`
— or with `sandbox.enabled` left at its default of `false` — the orchestrator
makes **no docker call at all**. Nothing about dispatch, teardown, or status
touches a container runtime unless a project's config asks for one explicitly.

When enabled, each slice's compose stack is published on its own
`127.0.0.x` loopback address and its own compose project, so parallel
worktrees never contend for a host port. Container-to-container traffic is
unaffected — only host-side publishing is rebound.

### Full schema

```yaml
sandbox:
  enabled: true                      # opt in; false (the default) means no docker call is ever made
  compose_file: docker-compose.yml   # path to the compose file, relative to the project root
  health_service: postgres           # optional; a compose service to await via `docker compose ps`
  health_timeout: 60                 # seconds to wait for health_service to report healthy
  env:
    pg_dsn: "postgresql://user:pass@{ip}:5432/db"
    qdrant_url: "http://{ip}:6333"
  teardown:
    on_verified_closed: volumes      # volumes | containers | none
    on_failed: containers
```

| Property | Type | Description |
|----------|------|-------------|
| `enabled` | bool | Opt-in switch. `false` (the default) means the orchestrator never invokes docker. |
| `compose_file` | string | Path to the `docker compose` file, relative to the project root. |
| `health_service` | string or null | A compose service name to poll with `docker compose ps` before the dispatched agent is allowed to proceed. Omit (`null`) to skip the wait. |
| `health_timeout` | int | Seconds to wait for `health_service` to report `healthy` before dispatch fails with `SandboxError`. |
| `env` | mapping | Extra environment variables passed to the dispatched agent. Values may reference the two template tokens below and `${VAR}` from the parent process environment. |
| `teardown.on_verified_closed` | string | Teardown mode run when a slice's status becomes `VERIFIED_CLOSED`. |
| `teardown.on_failed` | string | Teardown mode run when the dispatched agent exits non-zero (isolated agents only -- a non-isolated agent's failure never tears down the stack it merely attached to). |

### Template tokens

`sandbox.env` values may contain exactly two substitutions:

| Token | Expands to |
|-------|------------|
| `{ip}` | The loopback address allocated to this slice's branch (e.g. `127.0.0.78`). |
| `{project}` | The compose project name derived from the branch. |

Any other `{...}` token — including a typo like `{IP}` — is rejected with
`ConfigError` at load time, before any agent is dispatched. `${VAR}` expands
first, from the parent process environment, so a real credential can be
sourced from `.env` rather than committed to a tracked config file.

**`LOOPBACK_IP` and `COMPOSE_PROJECT_NAME` are injected unconditionally and are
not declarable.** They are the contract every compose file and hook can rely
on, not a setting a project chooses — they are not keys you write under
`sandbox.env`, and the two tokens above are how you reference their values
inside your own `env` templates.

### Teardown modes

`TEARDOWN_MODES` is the enum both `teardown.on_verified_closed` and
`teardown.on_failed` must be one of:

| Mode | Destroys |
|------|----------|
| `volumes` | `docker compose down -v` — stops and removes containers, networks, **and volumes**, and releases the loopback address (the sandbox state record is deleted). |
| `containers` | `docker compose down` (no `-v`) — stops and removes containers, but keeps volumes and the state record, so a re-`up` returns the same address and the same data. |
| `none` | Nothing. The stack is left running untouched. |

The one state invariant: **the state record is deleted if and only if the
volumes are destroyed.** `containers` mode — the default for `on_failed` —
keeps a failure diagnosable; `volumes` mode — the default for
`on_verified_closed` — releases the address a closed slice no longer needs.

### `isolated_worktree` decides who gets a sandbox

There is no separate on/off switch per agent. Whether a dispatch brings a
stack up, attaches to an existing one, or touches nothing at all follows
directly from the agent's `isolated_worktree` setting:

| `isolated_worktree` | On dispatch |
|---|---|
| `true` | `sandbox.ensure_up(...)` — allocates an address if needed, brings the stack up, injects the environment. This agent owns the stack's lifecycle. |
| `false` | `sandbox.resolve_env(...)` — injects the environment **only if** a stack already exists for the current branch; never brings one up. |

A non-isolated agent runs on the human's own branch, so it attaches to
whatever stack is already there instead of starting a competing one.

### Fail closed on a missing address: `${LOOPBACK_IP:?}`

The project's own `docker-compose.yml` must publish ports through
`${LOOPBACK_IP:?}`, not a hardcoded `127.0.0.1` or a bare `${LOOPBACK_IP}`:

```yaml
services:
  postgres:
    ports:
      - "${LOOPBACK_IP:?}:5432:5432"
```

The `:?` suffix makes `docker compose` refuse to start if `LOOPBACK_IP` is
unset, rather than silently publishing on every interface or colliding with
another slice's stack on `127.0.0.1`. This is the compose file's half of the
contract — the orchestrator's half is injecting `LOOPBACK_IP` before every
`ensure_up`.

### CLI

Human-facing lifecycle control, exposed as a subcommand of the same entry
point used for dispatch — `orchestrator.py sandbox <action>`.

**Flags must precede the action.** `cmd` is parsed as `nargs=REMAINDER`
(needed so `exec -- <command>` can pass arbitrary flags through untouched),
which means anything after the action is swallowed as part of `exec`'s
command instead of being parsed as a flag:

```bash
# Correct: flags before the action
python -m scripts.orchestrator sandbox --project-root . status

# Wrong: rejected -- the flag is swallowed by `exec`'s REMAINDER bucket and the action fails closed
python -m scripts.orchestrator sandbox status --project-root .
```

Every action other than `exec` fails closed with a clear error if anything
lands after it, rather than quietly running with the wrong config.

| Action | What it does |
|--------|---------------|
| `up` | `sandbox.ensure_up(...)` — allocates an address if needed, brings the stack up, waits on `health_service` if configured. |
| `restart` | Tears down containers (keeping volumes), then brings the stack back up on the same address. |
| `status` | Lists every tracked stack: branch, loopback address, and `running`/`stopped` (from `docker compose ps`, not address availability). |
| `--shell posix\|powershell\|json env` | Prints the stack's environment in the requested format — `export KEY=VALUE`, `$env:KEY = "..."`, or a JSON object. |
| `exec -- <command...>` | Runs `<command...>` with the stack's environment merged in, in the project root. Everything after `--` is passed through untouched. |
| `--yes teardown` | Destroys containers and volumes and deletes the state record. Refuses (exit code 2) without `--yes`. |

All actions accept `--project-root <dir>` (default: cwd; `--dir` is the older name for it) and `--branch
<branch>` (default: current branch). `up`, `restart`, `env`, `exec`, and
`teardown` all require `sandbox.enabled: true` in `.superpowers/agents.yaml`
— against a disabled or absent `sandbox` block, `up`/`restart`/`teardown`
print a clear error and exit non-zero rather than crashing or claiming
success, and `env`/`exec` report "no sandbox state" for the branch.
