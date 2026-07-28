---
slice_id: "slice-04-distribution-and-commands"
title: "Distribution and commands: make the plugin installable, and make the operating procedure executable"
status: DRAFT_SPEC
target_version: "2.3.0"
depends_on: []
---

# Slice 04 — Distribution and Commands

## 1. Problem

Slice 03 shipped an operating procedure that tells a Claude Code session how to
drive the lifecycle without the human touching a status by hand. It landed at
`9750a81` and changed nothing, because the plugin that carries it is not
installed anywhere. This slice closes that gap and removes the one mechanism in
the procedure that still depends on the model guessing.

Every finding below was read out of the tree at `9750a81` and out of the live
Claude Code installation on this machine, not inferred from the docs.

### 1.1 There is no way to install this plugin

`.claude-plugin/plugin.json` is a valid manifest. `skills/`, `hooks/`, and
`scripts/` are laid out exactly as `plugin-dev/plugin-structure` requires; a
manual audit against that skill's rules found no violation. The plugin is
nonetheless absent from `~/.claude/plugins/installed_plugins.json`.

That is not an oversight. Claude Code installs plugins through marketplaces:
`known_marketplaces.json` records the marketplace, a
`.claude-plugin/marketplace.json` in the marketplace repository lists the
plugins, and `installed_plugins.json` records the result. All seven plugins
installed on this machine — `gopls-lsp`, `pydantic-ai`, `superpowers`,
`mcp-server-dev`, `frontend-design`, `claude-md-management`, `plugin-dev` —
arrived through `claude-plugins-official`. There is no non-marketplace install
in evidence.

This repository ships no `marketplace.json`. `README.md:260`, under the heading
"Installation", says:

```
git clone https://github.com/ramil-zakirov-dev/superpowers-multiagents.git
cd superpowers-multiagents
pip install -r requirements.txt
```

That is a source checkout. It makes the orchestrator runnable as a Python
program; it does not install a plugin, and no amount of it will put the skill,
the hook, or anything else in front of a Claude Code session.

The consequence is exact and already paid for once: the `Operating procedure`
table at `skills/multiagent-orchestrator/SKILL.md:154-178` — eleven rows
declaring who decides and what runs — is inert text in the only environment
obliged to obey it. Slice 03's own acceptance criterion §7.1 recorded this and
it is still open.

### 1.2 The one path the model has to compute

`skills/multiagent-orchestrator/SKILL.md:8-15` opens with:

> The orchestrator ships with this plugin, not with the user's project. Resolve
> it as `<skill base directory>/../../scripts/orchestrator.py` — the base
> directory is announced when this skill is loaded.

Every invocation in the procedure table depends on the model performing that
relative traversal correctly and re-using the result. It is the only step in the
whole lifecycle whose correctness rests on the model doing arithmetic rather than
on a guard, and it fails silently in the ordinary way: a wrong path produces "no
such file", the model retries with another guess, and the transition the human
just approved does not happen.

A plugin command is not subject to this. `${CLAUDE_PLUGIN_ROOT}` inside an
inline `` !`…` `` block is expanded by the harness before the model sees the
prompt. The path stops being a thing anyone computes.

### 1.3 The procedure asks the human not to type, and then prints what to type

The procedure's own preamble states the goal: *"The human always decides, and
never types."* Eight of its eleven rows then give a bare subcommand —
`set-status --file <plan> --status VERIFIED_CLOSED`, `milestone new --id <id>
--title "<title>"`, and so on. Bare, meaning not runnable: the reader must
prepend `python` and the path from §1.2 to each one before anything happens.

So the two defects compound. The table is the document that says what to run,
and it is the one place that does not say how to run it; the missing half is
exactly the half no guard covers.

## 2. Goals and non-goals

### 2.1 Goals

1. The plugin can be installed into Claude Code by the ordinary marketplace
   route, and **is** installed — verified against `installed_plugins.json`, not
   asserted.
2. Every row of the operating procedure that carries an invocation is invocable
   as a plugin command, by the human typing it or by the model calling it.
3. The invocation inside those commands cannot be misresolved: the orchestrator
   path is expanded by the harness, not derived by the model.
4. The mapping between procedure rows and command files is machine-checked in
   both directions, so a row without a command — or a command without a row — is
   a test failure rather than documentation drift.

### 2.2 Non-goals

- **No new orchestrator behaviour.** The commands are wrappers over the CLI
  surface that exists at `9750a81`. No subcommand is added, none changes
  meaning. In particular there is no `approve` verb that infers the target
  status from the document's current state: it would save little and hide which
  gate the human is standing at, and a HITL gate whose subject is implicit is a
  worse gate.
- **No command chains an observable row onto a human row.** `approve-spec`
  performs its transition and stops. One command, one task; and a paid
  background dispatch is a surprising side effect to bury inside a command
  named "approve". This does not *prevent* the dispatch: the procedure landed in
  slice 03 says observable rows run without being asked, so a session following
  it will dispatch the planner next. The command neither performs that nor
  forbids it. (Worth the owner's attention separately: under that rule,
  approving a spec spends money without a further keystroke. It is slice 03's
  contract and this slice does not reopen it.)
- **`sandbox`, `summary`, `trigger-hook`, `milestone sync` and `milestone check`
  get no commands.** They are not rows of the procedure. `sandbox` additionally
  takes flags before its action and an `exec -- <cmd>` tail that does not survive
  positional substitution.
- **`--model` stays CLI-only.** An optional third positional would substitute as
  `--model ""` when omitted.
- **`python`, not `python3`.** Every existing document in this repository
  invokes `python`; on a machine where only `python3` exists, all of them are
  already broken, and the commands would be broken identically. Fixing it means
  a resolution shim and touching every document — a separate concern, recorded
  here so it is a known debt rather than a discovery.
- **`package.json` and the leftover `.superpowers/sdd/` directory are not
  touched.** `package.json` carries no JavaScript and duplicates the version,
  but `tests/test_docs_consistency.py:357` already pins the two together, so it
  is inert weight rather than a drift risk.

## 3. Design

### 3.1 The marketplace descriptor

A new `.claude-plugin/marketplace.json`, alongside the existing `plugin.json`,
turns this single-plugin repository into its own marketplace:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "ramil-zakirov-dev",
  "description": "Ramil Zakirov's Claude Code plugins",
  "owner": { "name": "Ramil Zakirov" },
  "plugins": [
    {
      "name": "superpowers-multiagents",
      "description": "Multi-agent orchestration for Superpowers workflows: a configurable N-level agent hierarchy with supervised background execution and Markdown-frontmatter lifecycle state",
      "author": { "name": "Ramil Zakirov" },
      "category": "development",
      "source": {
        "source": "github",
        "repo": "ramil-zakirov-dev/superpowers-multiagents"
      },
      "homepage": "https://github.com/ramil-zakirov-dev/superpowers-multiagents"
    }
  ]
}
```

The marketplace is named `ramil-zakirov-dev`, not `superpowers-multiagents`.
Installation is addressed as `<plugin>@<marketplace>`; naming both the same
produces `superpowers-multiagents@superpowers-multiagents`, which reads as a
mistake. `category: development` is drawn from the vocabulary the official
marketplace actually uses.

The plugin entry carries no `version`. Version comes from the manifest at the
source, which is where `plugin.json` already declares it; a second copy would be
a second thing to forget.

**This requires the branch to be pushed.** `origin/main` is at `ce3fbce`;
local `main` is fifteen commits ahead. A `source: github` descriptor pointing at
an unpushed tree installs a version of the plugin that predates slice 03. The
push is part of this slice's acceptance, not a follow-up.

### 3.2 The command set

Eight files in `commands/`, flat — `plugin-dev/command-development` puts the
threshold for namespaced subdirectories at fifteen. The directory needs no
declaration in `plugin.json`; `commands/` is auto-discovered.

Plugin commands are namespaced by the harness under the plugin name:
`plugin-dev` ships `commands/create-plugin.md` and it surfaces as
`plugin-dev:create-plugin`. So no manual prefix is needed, `/status` cannot
collide with anything, and the form actually typed is
`/superpowers-multiagents:status`. Documentation uses that full form
throughout — it is what a person types, and a shorter form in a document that
does not work when typed is worse than a long one.

| File | Command | Wraps | Procedure row |
| :--- | :--- | :--- | :--- |
| `status.md` | `status` | `status --dir docs/superpowers` | (reads state; not a transition) |
| `new-milestone.md` | `new-milestone <id> <title…>` | `milestone new --id --title` | A milestone is agreed on |
| `activate-milestone.md` | `activate-milestone <brief>` | `set-status --status MILESTONE_ACTIVE` | The brief is written |
| `approve-spec.md` | `approve-spec <spec>` | `set-status --status SPEC_APPROVED` | A slice spec is drafted |
| `approve-plan.md` | `approve-plan <plan>` | `set-status --status PLAN_APPROVED` | The plan is audited |
| `close-slice.md` | `close-slice <plan>` | `set-status --status VERIFIED_CLOSED` | The diff is audited |
| `close-milestone.md` | `close-milestone <brief>` | `set-status --status MILESTONE_CLOSED` | Every track is complete |
| `dispatch.md` | `dispatch <role> <file>` | `dispatch-agent --role --file` | Spec approved / Plan approved |

`status` has no procedure row of its own; it is the command the human and the
model both reach for before deciding anything, and leaving it out would mean the
one zero-argument, zero-risk invocation stayed hand-typed.

### 3.3 The shape of a command

Each command executes its invocation inline and then tells the model what to do
with the result. `approve-spec.md` in full:

```markdown
---
description: Record the human's approval of a slice design spec
argument-hint: [path-to-spec]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status SPEC_APPROVED`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line. What happens next is governed by the
operating procedure, not by this command.

If it failed, the orchestrator refused and its message says why. Report the
reason. Do not retry with a different status, a different file, or a direct
edit of the frontmatter.
```

Three properties follow from the inline form, and they are the reason for it:

- **The invocation is not the model's to compose.** The harness expands
  `${CLAUDE_PLUGIN_ROOT}` and substitutes `$1` before the prompt exists. The
  model receives a result, not a command to construct.
- **A refusal cannot be worked around.** The orchestrator validates transitions
  and fails closed. The prompt's final paragraph exists because the model's
  instinct on a non-zero exit is to try another way; there is no other way that
  is legitimate.
- **The mutation fires before the model reasons.** That is safe precisely
  because the orchestrator is the guard: it is what refuses an illegal
  transition, a dirty tree, or a wrong document kind, and it does so whether the
  model was going to check or not.

`close-slice` merges the feature branch and re-syncs every milestone brief that
lists the slice. Its body states both effects.

### 3.4 One command parses its own arguments

`new-milestone` is the only command taking free text: `milestone new` requires
`--title`, and a title is several words. Positional substitution splits on
whitespace, and whether quoting survives it is documented nowhere in
`plugin-dev/command-development` — the reference files give examples and no
rule. Depending on undocumented behaviour for the first command in the lifecycle
is not acceptable, so the split is performed where the semantics are defined:

```
!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" milestone new \
  --id "$(echo "$ARGUMENTS" | cut -d' ' -f1)" \
  --title "$(echo "$ARGUMENTS" | cut -d' ' -f2-)"`
```

`$ARGUMENTS` is documented as the whole argument string. `cut` splits it at the
first space in bash, which has defined semantics. The model still composes
nothing.

This is also the one command whose `allowed-tools` is not just
`Bash(python:*)`: the substitutions are `echo` and `cut` pipelines, so it
declares `Bash(python:*), Bash(echo:*), Bash(cut:*)`.

### 3.5 What the operating procedure shows

The table at `SKILL.md:154-178` replaces each bare subcommand with its command
token, so a row reads:

```
| The diff is audited | Human approves | `/superpowers-multiagents:close-slice <plan>` |
```

The three rows the supervisor performs keep their parenthesised notes and gain
nothing. The raw CLI does not disappear either: `SKILL.md` §§1–8 ("Orchestrator
Commands (CLI Integration)") already documents the whole surface and stays as
written. The division is that the table says *what to run* and the reference
section says *what the CLI is*.

The alternative — a second column carrying both forms — was rejected. It offers
the model a choice between two ways to do one thing, and a choice is variance.

The path-resolution paragraph at `SKILL.md:8-15` **stays**. It looks like this
slice's natural casualty, but `milestone sync`, `milestone check`, `summary`,
`trigger-hook` and `sandbox` deliberately get no commands, and the model still
needs the path for them. `test_skill_resolves_the_orchestrator_by_an_absolute_or_anchored_path`
(`tests/test_docs_consistency.py:46`) remains in force.

`README.md`'s Installation section is rewritten around the marketplace route.
The clone survives as what it actually is: how to work on the plugin, not how
to use it.

## 4. Failure modes

| Situation | Behaviour |
| :--- | :--- |
| Command invoked with no argument, `dispatch` or `new-milestone` | `$1`/`$ARGUMENTS` substitutes empty; `dispatch-agent` and `milestone new` both validate their input and refuse with a clear message (`Target file '...' not found.`, `Error: milestone_id '' contains invalid characters.`). Fail-closed, with a message naming the problem. |
| Command invoked with no argument, one of the five `set-status` commands | `$1` substitutes empty; `cmd_set_status` calls `Path("").resolve()` then reads the file with no existence check first, so the orchestrator raises an unhandled `FileNotFoundError` (a missing path) or `PermissionError`/`IsADirectoryError` (an empty path resolving to a directory) instead of a clean refusal. **Not fail-closed with a message; a raw traceback.** This is a pre-existing gap in `cmd_set_status`, not something this slice introduces or is permitted to fix — §2.2 forbids new orchestrator behaviour. Recorded here as debt: a future slice should add the same existence check `dispatch-agent` and `milestone new` already have. Until then, `approve-spec.md`/`approve-plan.md`/`activate-milestone.md`/`close-slice.md`/`close-milestone.md`'s own prompt text ("the orchestrator refused and its message says why … do not retry") is optimistic for this specific failure — a traceback is not a message that says why, and a model reading one is more likely to do exactly what that paragraph forbids. |
| Illegal transition (e.g. `approve-plan` on a `DRAFT_SPEC` document) | The orchestrator's state machine refuses; exit non-zero; the command body forbids retrying by another route. |
| `close-slice` against a spec instead of a plan | Already handled at `9750a81`: `VERIFIED_CLOSED` is legal only from `EXECUTION_COMPLETE`, and merge-then-mark ordering means nothing is merged before the check. |
| Argument containing a double quote | Breaks out of the quoting and the shell sees something else. Accepted: the argument is typed by the human at their own terminal, or read by the model from a local document. There is no untrusted input path into a local dev tool. |
| Plugin installed but `python` absent from the harness's shell | Every command fails identically and visibly on the first line of output. See §2.2 — pre-existing and uniform across the repository. |
| Marketplace points at `main` before the push | The installed plugin is `ce3fbce`: no milestone kind, no `milestone` subcommand, and four of the eight commands wrap invocations the installed orchestrator rejects. This is why the push is acceptance, not follow-up. |

## 5. Testing

No test may invoke a real harness, a real container runtime, or the network.
The command files are data; they are read and asserted against, not executed.

**Marketplace descriptor.**

1. `marketplace.json` parses, and its single plugin entry's `name` and
   `description` equal the values in `plugin.json`.
2. `source.repo` equals the `owner/repo` of `plugin.json`'s `repository` URL —
   a mutation that edits one and not the other must go red.
3. The entry's `category` is one the official marketplace uses.
4. The marketplace `name` differs from the plugin `name`.

**Command set.**

5. Every file in `commands/` has parseable YAML frontmatter with a
   `description` and, where it takes arguments, an `argument-hint`.
6. Every command that runs the orchestrator does so through
   `${CLAUDE_PLUGIN_ROOT}` — a literal absolute path, a `~`, or a relative
   traversal anywhere in `commands/` is a failure.
7. Every `--status` value appearing in `commands/` is a member of the state
   machine defined in `scripts/config.py`. A typo'd status is caught here, not
   at the gate.
8. Every `--role` value appearing in `commands/` is a configured agent role.

**Row ↔ command correspondence — the guard that carries the slice.**

The table's third column holds either a `/superpowers-multiagents:<name> …`
token in backticks, or a parenthesised note for the rows the supervisor
performs. That is the grammar the guards parse.

9. Every `/superpowers-multiagents:<name>` token in the table resolves to an
   existing `commands/<name>.md`.
10. Every file in `commands/` except `status.md` is named by at least one row.
    Both directions, because either drift is the same defect.
11. No row's action cell contains a bare orchestrator subcommand — that is, none
    of the subparser names registered in `scripts/orchestrator.py`
    (`set-status`, `dispatch-agent`, `milestone`, …) appears there outside a
    command token. This is what catches a row left half-migrated, which is the
    likely shape of the mistake.

**Mutation checks the implementer must run and report.** Slice 03 shipped two
defects that a 303-green suite did not see, both because a test asserted
something weaker than it appeared to. For guards 9, 10 and 11, delete the guard
and confirm the suite goes red; if it stays green the guard is decorative.

## 6. Files

| File | Change |
| :--- | :--- |
| `.claude-plugin/marketplace.json` | Create |
| `commands/status.md` | Create |
| `commands/new-milestone.md` | Create |
| `commands/activate-milestone.md` | Create |
| `commands/approve-spec.md` | Create |
| `commands/approve-plan.md` | Create |
| `commands/close-slice.md` | Create |
| `commands/close-milestone.md` | Create |
| `commands/dispatch.md` | Create |
| `skills/multiagent-orchestrator/SKILL.md` | Rewrite the procedure table's invocations as commands; add a short section naming the command set |
| `README.md` | Rewrite Installation around the marketplace; document the command set; add `commands/` to the repository-structure listing |
| `docs/architecture.md` | Record `commands/` as a component |
| `.claude-plugin/plugin.json` | Version → `2.3.0` |
| `package.json` | Version → `2.3.0` (kept in step by the existing guard) |
| `tests/test_docs_consistency.py` | Guards 1–11 |

## 7. Acceptance

Automated tests are necessary and not sufficient here: the whole point of the
slice is that something works in a live installation, and slice 03 demonstrated
that a landed-but-uninstalled layer changes nothing.

1. The full suite is green, and guards 9–11 have been shown to fail when
   deleted.
2. `main` is pushed. `origin/main` contains this slice.
3. The marketplace is added and the plugin installed by the ordinary route. The
   plugin appears in `~/.claude/plugins/installed_plugins.json`.
4. In a fresh Claude Code session, the plugin's commands are listed.
5. `status` returns the real state of a project directory.
6. `new-milestone` with a **two-word title** creates a brief whose frontmatter
   title is intact. This is the live check on §3.4: the argument split is
   asserted in tests only as text, and only a real invocation proves
   `$ARGUMENTS` reaches bash whole.
7. `approve-spec` against a document in a state that forbids it is refused, and
   the session reports the refusal instead of routing around it.
8. One transition is driven end-to-end by command only, with the human typing
   no path.

Items 3–8 are the owner's to run; they need a live Claude Code and an install.
