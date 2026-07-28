---
slice_id: "slice-03-milestone-contract"
title: "Milestone contract: a second document kind with a PRD-shaped brief, its own lifecycle, and machine-owned track state"
status: DRAFT_SPEC
target_version: "2.2.0"
depends_on: []
---

# Slice 03 — Milestone Contract

## 1. Problem

The orchestrator claims a four-level hierarchy — Milestone ➔ Track ➔ Slice ➔
Plan ➔ Code — but implements exactly one document kind. The milestone is
referenced by three code paths and defined by none of them. Every finding below
was read out of the current tree at `ce3fbce`, not inferred from the docs.

### 1.1 Agent 1 has no contract

`skills/multiagent-orchestrator/SKILL.md:23` is the entire definition of the
milestone artefact: *"High-level release objective managed by Agent 1 (Fable 5)
+ Human"*, plus a path, `docs/superpowers/milestones/YYYY-MM-DD-milestone-N.md`.

There is no template, no required content, no frontmatter schema, no status, and
nothing that validates any of it. The one operational instruction attached to
milestones — SKILL.md:123, *"check off `[x]` in the corresponding Track"* — is a
manual edit no code reads or verifies.

The consuming project has no `docs/superpowers/milestones/` directory at all.
Two slices have shipped without one.

### 1.2 Milestones are supported in appearance only

Three code paths already assume milestone files exist:

| Path | What it does with a milestone |
| :--- | :--- |
| `orchestrator.py:65` (`cmd_status`) | Scans `milestones/*.md`, prints `status` and `title` from frontmatter |
| `dependencies.py:12` | Includes `milestones/` in the dependency search, so a slice may declare `depends_on: [milestone-1]` |
| `frontmatter.py` | Parses `milestone_id` — it appears in the test fixture at `tests/test_orchestrator.py:26` |

So a milestone is already expected to carry a status, and that status already
gates slice dispatch through `check_unmet_dependencies`. Nothing defines what
the status may be.

### 1.3 Every mutating path assumes the document is a slice

There is no notion of document kind anywhere. The consequences are concrete:

**`set-status` validates a milestone against the slice state machine.**
`cmd_set_status` loads `state_machine.valid_statuses` and applies it to whatever
file it was pointed at. A milestone can legally be moved to `EXECUTING` or
`PLAN_APPROVED` — states that describe a dispatched agent that does not exist
for a milestone.

**`VERIFIED_CLOSED` on a milestone attempts a git merge.**
`orchestrator.py:111` derives `slice_id = frontmatter.get("slice_id", filepath.stem)`.
For a milestone there is no `slice_id`, so the file stem is used and
`merge_and_cleanup_worktree` tries to merge a branch named
`feat/2026-07-28-milestone-1`. It fails — but with a git error about a missing
branch, which describes neither the cause nor the fix.

**`dispatch-agent` will run an agent against a milestone.**
`orchestrator.py:222` derives `slice_id` the same way, and the only gate is
`allowed_statuses` (`orchestrator.py:225-229`). A milestone sitting at
`SPEC_APPROVED` — reachable today, per the paragraph above — passes that gate.
The planner is dispatched against the milestone brief, and a worktree named
after the milestone file is created.

### 1.4 The instruction layer is inert in the environment that must obey it

`installed_plugins.json` lists six plugins; `superpowers-multiagents` is not
among them, and no marketplace entry points at it. The skill is never loaded in
the Claude Desktop sessions that are supposed to follow it, so every instruction
this slice writes into `SKILL.md` is dead text until the plugin is installed.

This is a deployment precondition, not a design defect, but it decides whether
the slice changes anything in practice. It is therefore an acceptance criterion
(§7), not a footnote.

## 2. Goals and non-goals

**Goals.**

1. The milestone becomes a declared document kind with its own lifecycle,
   distinct from the slice's, so no command can misread one for the other.
2. The milestone brief has an enforced shape — PRD-form sections whose presence
   is checked at the approval gate.
3. Track state is machine-owned: checkboxes are derived from the actual statuses
   of the slices a track lists, never hand-ticked.
4. Both existing holes in §1.3 fail closed with an accurate message.
5. `SKILL.md` carries an operating procedure that tells Claude Desktop which
   command to run at each transition, so the human decides but never types.

**Non-goals — not in this slice, deliberately.**

- **A track is not a document.** It stays a section inside the milestone brief.
  Making it a third file kind normalises a group of slices into an entity that
  must be created and closed by hand, for no gain.
- **No drift detector.** A command that compares recorded status against git
  reality (branch merged while the spec still reads `DRAFT_SPEC`) is valuable
  and separable; it needs its own git scanning and its own slice.
- **No change to the slice state machine.** Its statuses, transitions, and the
  `"VERIFIED_CLOSED"` string literal at `orchestrator.py:105` stay as they are.
- **No `agents.yaml` surface.** This slice adds no configuration key and needs
  no migration of any existing config file.

**Non-goals — rejected outright, not deferred.**

- **A generic `document_kinds` registry.** Two kinds exist and no third is in
  view. Generalising would push slice semantics — including merge-on-close,
  which does not reduce to configuration — into config, in a published plugin
  where that is a one-way door.
- **A `Risks & mitigations` section in the brief.** In LLM-authored documents it
  reliably degenerates into filler ("risk: it may not work"). Its honest content
  is already covered by `Open questions`.

## 3. Design

### 3.1 Document kind is declared, never inferred

A milestone brief declares `kind: milestone` in its frontmatter. The field is
written by `milestone new`. When `kind` is absent the document is a **slice** —
so every existing spec and plan keeps its current meaning and nothing migrates.

One fail-closed guard covers the authoring mistake this invites. If a file's
parent directory is named `milestones` and its frontmatter does not declare
`kind: milestone`, then `set-status` and `dispatch-agent` refuse:

```
Error: docs/superpowers/milestones/2026-07-28-milestone-1.md is in milestones/
       but does not declare `kind: milestone` in its frontmatter.
       Add it, or move the file if it is a slice document.
```

This is not inferring kind from location. It is detecting a contradiction
between two signals and stopping — which converts today's silent nonsense
(§1.3) into one accurate sentence.

The converse is not guarded: a document that correctly declares
`kind: milestone` works wherever it is stored. Location is a convention, not a
load-bearing input.

### 3.2 The milestone lifecycle

Three states. No agent is ever dispatched against a milestone, so there is no
exit code to derive a terminal status from, and therefore no `FAILED`.

```
MILESTONE_DRAFT ⇄ MILESTONE_ACTIVE → MILESTONE_CLOSED
```

| Transition | Gate |
| :--- | :--- |
| `DRAFT → ACTIVE` | Every required section present and non-empty (§3.3) |
| `ACTIVE → DRAFT` | None — reopening for editing is always allowed |
| `ACTIVE → CLOSED` | Every slice listed in every track is `VERIFIED_CLOSED` |
| `CLOSED → *` | None. Terminal, as `VERIFIED_CLOSED` is for slices |

`MILESTONE_CLOSED` is a human decision, never derived. The gate refuses a
premature close; it never performs one. "Every slice shipped" and "the objective
was met" are different claims, and auto-closing would make `Success metrics`
decorative. Descoping is done by removing the slice from the track list, which
is an explicit, reviewable edit.

**This state machine is fixed, not configurable — a deliberate departure from
the plugin's usual line.** Both of its gates are keyed to specific status names.
A project that renamed `MILESTONE_CLOSED` would silently detach the gate from
the transition. The slice machine already demonstrates the hazard: it is
advertised as configurable while `orchestrator.py:105` compares against the
literal `"VERIFIED_CLOSED"`. Reproducing that defect in a second place is worse
than declining the configurability.

### 3.3 The brief: PRD form, eight sections

The artefact is a **milestone brief in PRD form** — the documentation must say
it that way. It borrows the PRD spine because the section names carry a dense
prior for the LLM that authors and reads them; it is not a product-management
PRD, and `Track decomposition` is ours, not PRD's.

The template opens with an altitude statement, because "high-level yet thorough"
is a tension an LLM author resolves by sliding into slice-level detail:

> A milestone is 1–3 months of work and 2–5 tracks. If you are describing a
> screen or an endpoint, you are in the wrong document — that belongs in a slice
> spec.

Required sections, in the order the template generates them:

| Section | Template hint (prescriptive, not descriptive) |
| :--- | :--- |
| `Problem` | Whose pain, and why now. Include what exists today and why it is insufficient. No solution. |
| `Users` | Who, in which roles. If this milestone is internal infrastructure, name the engineering roles it serves and say so in one line — an honest short answer beats invented personas. |
| `Goals` | What becomes true when the milestone is met. One numbered goal per line. |
| `Non-goals` | Two labelled groups: **Not in this milestone** (sequencing) and **Rejected outright** (a stance that outlives the milestone). 3–7 items; each names something a reasonable person would expect and we are deliberately not doing. |
| `Success metrics` | A table with one row per goal above — columns `Goal` and `How we will know`. Add an `Overall` row for milestone-wide measures. |
| `Constraints & invariants` | What must not be violated. One line each. |
| `Track decomposition` | One sentence on why this decomposition and not another. Then a track per subsystem, each with `depends_on:` naming the tracks it needs or `—`. Slices are listed by `slice_id`; a slice that does not exist yet is expected and normal. |
| `Open questions` | What is unresolved, each with the name of whoever decides it. |

**Section hints are prescriptive on purpose.** The completeness check can only
observe presence, so the hint text is the only thing steering quality.
`<!-- Non-goals: 3–7 items; each names something a reasonable person would
expect and we are deliberately not doing -->` produces content;
`<!-- What we're not doing -->` produces a blank.

**Completeness check.** A section counts as present when a level-2 heading whose
text, after stripping surrounding whitespace, equals the required name exactly —
case and `&` included — exists. It counts as non-empty when at least one line
beneath it, up to the next heading of any level, is neither blank, nor an HTML
comment, nor a heading.
Template hints are HTML comments, so an untouched section reads as empty. Order
is not enforced: the template supplies it, and enforcing it would add a failure
mode that buys nothing.

**Known limit, stated rather than sold.** The check cannot distinguish a real
success metric from a plausible-looking one. `Success metrics` is the section
where the gate is theatre and the value rests entirely on the author's honesty.
It stays required because forcing the question is worth it, but it is not a
guarantee.

### 3.4 Track state is machine-owned

Two sources of truth, cleanly split:

- **The brief owns membership.** A track lists its slices by `slice_id`. This
  direction — rather than slices declaring a `track_id` and tracks being
  assembled by scanning — is required by what a milestone is: a *planning*
  document must be able to name slices that do not exist yet. The scanning
  direction can only ever display what has already been written, losing exactly
  what decomposition is for.
- **Slice files own status.** A `slice_id` is resolved to a file by the same
  rule `dependencies.py:_resolve` already uses: frontmatter `slice_id` first,
  filename stem second, ambiguity is an error rather than a guess.

`milestone_id` may still appear in a slice's frontmatter as a human-readable
back-reference. It is never an input to membership.

**The machine-owned region.** The orchestrator writes only between markers,
which `milestone new` generates:

```markdown
## Track decomposition

Split by ownership boundary: intake is gateway-shaped, billing is ledger-shaped.

<!-- tracks:begin -->
### track-1: Intake
depends_on: —
- [ ] slice-01-gateway — not yet specced
- [x] slice-02-native-sandbox — VERIFIED_CLOSED · Native sandbox: per-slice isolation

### track-2: Billing
depends_on: track-1
- [ ] slice-04-ledger — DRAFT_SPEC · Ledger write path
<!-- tracks:end -->
```

**Line grammar.** `- [<x| >] <slice_id>[ — <machine suffix>]`, where the
separator is an em-dash surrounded by single spaces. A `slice_id` can never
contain that separator: `utils._sanitize_id` admits only alphanumerics, hyphens,
underscores and dots, so splitting the line is unambiguous.

**What the sync may rewrite, stated exhaustively.** Outside the markers: nothing
at all. Inside them: only the checkbox character and the suffix of a list item.
Track headings, `depends_on:` lines, blank lines, and any prose between tracks
are reproduced verbatim, as is the order of everything. The human owns the
`slice_id`; the machine owns the checkbox and everything after the separator.
Because the suffix is regenerated from scratch on every run rather than patched,
idempotency follows from construction instead of being separately guaranteed.

The suffix is `<STATUS> · <title from the slice's frontmatter>` when the slice
resolves, and `not yet specced` when it does not. The checkbox is `x` if and
only if the resolved status is `VERIFIED_CLOSED`.

**Sync runs automatically where it matters.** After `set-status --status
VERIFIED_CLOSED` succeeds on a *slice*, the orchestrator re-syncs every
milestone brief that lists that `slice_id`. Closing a slice and updating the
milestone are one command, so a checkbox cannot go stale and nobody has to
remember a step. A sync failure is reported as a warning and does not overturn
the close — the same discipline the existing code applies to the
`on_slice_verified_closed` hook and to sandbox teardown (`architecture.md`,
"Sandbox lifecycle"): the outcome was already recorded, and a later step must
not retroactively unrecord it.

The milestone directory to scan is resolved the way `dependencies._candidate_dirs`
resolves siblings, so the two agree by construction.

### 3.5 Command surface

Three subcommands under `milestone`:

```bash
python "<orchestrator>" milestone new --id milestone-1 --title "Intake automation" [--dir docs/superpowers]
python "<orchestrator>" milestone sync --file docs/superpowers/milestones/2026-07-28-milestone-1.md
python "<orchestrator>" milestone check --file docs/superpowers/milestones/2026-07-28-milestone-1.md
```

`new` writes `<dir>/milestones/<YYYY-MM-DD>-<id>.md` from the template, with
`kind: milestone`, `milestone_id`, `title`, and `status: MILESTONE_DRAFT` in the
frontmatter. `--id` is validated by the existing `utils._sanitize_id`. An
existing target file is never overwritten: the command refuses and exits
non-zero.

`sync` rewrites the machine-owned region. `check` runs the completeness check,
prints each missing or empty section, and exits non-zero if any are.

Unlike `sandbox`, these take flags after the action in the ordinary way. The
`sandbox` command's flags-first constraint exists only because `exec --` needs
`argparse.REMAINDER`; nothing here passes a command through, so nothing here
inherits that quirk. The documentation must not copy the warning across.

### 3.6 Changes to existing commands

| Command | Change |
| :--- | :--- |
| `set-status` | Routes by kind. For a milestone: the fixed milestone machine, its two gates, and **no merge, no worktree cleanup, no sandbox teardown**. For a slice: unchanged, plus the auto-sync of §3.4 after a successful `VERIFIED_CLOSED`. Both: the §3.1 guard. |
| `dispatch-agent` | Refuses `kind: milestone` before any gate or lock: no role operates on a milestone. Plus the §3.1 guard. |
| `dependencies.py` | "Closed" becomes a predicate over kind: `MILESTONE_CLOSED` for a milestone, `VERIFIED_CLOSED` for a slice. Without this, the already-supported `depends_on: [milestone-1]` would read a correctly closed milestone as permanently unmet. |
| `cmd_status` | Milestones show their real status plus track progress: `[MILESTONE_ACTIVE ] 2026-07-28-milestone-1.md - Intake automation (3/7 slices closed)`. Slices are unchanged. |

No status name is added to `state_machine.valid_statuses`. A slice therefore
cannot be set to `MILESTONE_CLOSED` and a milestone cannot be set to
`EXECUTING`: each machine rejects the other's vocabulary at no extra cost.

### 3.7 The operating procedure

`SKILL.md` gains an **Operating procedure** section written as obligations, not
as a feature list. It states, for every transition of both kinds, what triggers
it, who decides, and the exact command Claude Desktop must run.

The governing distinction, stated in the section itself: **the human always
decides, and never types.** Transitions whose trigger is an observable fact are
run by Claude without being asked. Transitions that exist because a human
approved something — `SPEC_APPROVED`, `PLAN_APPROVED`, `VERIFIED_CLOSED`,
`MILESTONE_ACTIVE`, `MILESTONE_CLOSED` — are run by Claude *immediately after*
the human's approval, never in place of it. Automating the keystroke is the
goal; automating the judgement would dissolve the gate, including the one that
catches an executor which halted on a blocker and still exited 0
(`architecture.md`, "What the exit code cannot tell you").

Prose is the softest of the three enforcement layers, and it is the only one
that covers Agent 1 and Agent 2. The other two are code: the gates in §3.2 and
§3.3 refuse illegal transitions outright, and the auto-sync in §3.4 removes a
step that could otherwise be forgotten.

## 4. Failure modes

Every case fails closed and names its remedy.

| Situation | Behaviour |
| :--- | :--- |
| `slice_id` in a track resolves to several files | Error, exit non-zero, **nothing written** — matching `dependencies.py`'s refusal to guess |
| `slice_id` resolves to no file | Not an error. Rendered `- [ ] <id> — not yet specced` |
| Track markers missing from a milestone brief | `sync` refuses and names the two markers to add |
| A line inside the markers does not match the grammar | Refuse; never reinterpret a line the author may have meant differently |
| `DRAFT → ACTIVE` with an empty section | Refused, listing every offending section at once rather than one per attempt |
| `ACTIVE → CLOSED` with an unclosed slice | Refused, listing each unclosed slice and its current status |
| File in `milestones/` without `kind: milestone` | `set-status` and `dispatch-agent` refuse (§3.1) |
| `milestone sync` or `check` aimed at a file that is not `kind: milestone` | Refused. These commands operate on one document kind and must not silently treat a slice spec as a brief |
| `dispatch-agent` against `kind: milestone` | Refused before the lock is taken, so no artefact is left behind |
| Auto-sync fails after a slice closed | Warning only. The close stands |
| `milestone new` target already exists | Refused; no file is overwritten |

## 5. Testing

No test may invoke a real harness, a real container runtime, or a network. All
git interaction uses throwaway repositories under the test's `tmp_path`.

Red-first coverage, by area:

**Kind routing.** A milestone in `milestones/` without `kind` is refused by both
`set-status` and `dispatch-agent`. A declared milestone outside `milestones/`
works. `dispatch-agent --role planner` against a milestone is refused, and no
lock file and no worktree exist afterwards.

**Milestone machine.** Each legal transition succeeds; each illegal one is
refused with the status unchanged on disk. `MILESTONE_CLOSED` is terminal.
`EXECUTING` is refused on a milestone and `MILESTONE_CLOSED` on a slice.
`set-status` on a milestone performs no git operation — asserted by pointing it
at a repository where any merge would fail.

**Completeness check.** Each required section, removed individually, is
reported. A section containing only its HTML-comment hint counts as empty. A
section with one prose line counts as filled. All offending sections are
reported in one run.

**Track sync.** Checkbox and suffix are derived from real slice frontmatter.
Two consecutive syncs produce byte-identical files. Text before, between, and
after the markers is preserved exactly, including a second `## ` section after
the region. Ambiguous `slice_id` aborts with nothing written. Unresolvable
`slice_id` renders `not yet specced` and is not an error.

**Auto-sync.** Closing a slice that a brief lists updates that brief in the same
command. A brief that does not list it is untouched. A sync that raises leaves
the slice closed and emits a warning.

**Dependency gate.** A slice depending on a `MILESTONE_CLOSED` milestone
dispatches; on a `MILESTONE_ACTIVE` one it is blocked.

**Docs consistency.** Extend `tests/test_docs_consistency.py`: every milestone
status and every `milestone` subcommand appears in the shipped documentation;
the required-section list in the docs matches the list the code enforces; and
the `sandbox` flags-first warning has not been copied onto `milestone`.

## 6. Files

| File | Responsibility |
| :--- | :--- |
| `scripts/milestone.py` (new) | Kind detection, the fixed state machine, the completeness check, the region parser/renderer, and the template. One module, one document kind. |
| `scripts/orchestrator.py` | The `milestone` subparser and three handlers; kind routing in `cmd_set_status` and `cmd_dispatch_agent`; the auto-sync call; track progress in `cmd_status`. |
| `scripts/dependencies.py` | The kind-aware closed predicate. |
| `tests/test_milestone.py` (new) | The unit surface of `milestone.py`. |
| `tests/test_milestone_cli.py` (new) | The three subcommands end to end. |
| `tests/test_milestone_routing.py` (new) | Kind routing through `set-status` and `dispatch-agent`, and the auto-sync. |
| `tests/test_docs_consistency.py` | The guards named in §5. |
| `skills/multiagent-orchestrator/SKILL.md`, `README.md`, `docs/configuration.md`, `docs/architecture.md` | The hierarchy, the operating procedure, the brief's shape, and the second lifecycle. |

## 7. Acceptance

1. The full suite is green, and every guard listed in §5 has been shown to fail
   before its implementation existed.
2. **The plugin is installed in Claude Desktop and `multiagent-orchestrator`
   loads in a fresh session.** Until this holds, §3.7 ships as text nothing
   reads (§1.4), and the slice would close without changing how the work is
   actually done.
3. A real milestone brief is created with `milestone new`, `DRAFT → ACTIVE` is
   observed to be **refused** while a section is empty and to succeed once it is
   filled, and a `sync` renders at least one resolved slice and one not-yet
   specced slice.
4. Closing a real slice through `set-status --status VERIFIED_CLOSED` is
   observed to tick that slice's checkbox in the brief without a separate
   command.

Authoring the actual content of the consuming project's milestones is product
work for its owner, not part of this slice.
