---
slice_id: "slice-04-distribution-and-commands"
title: "Distribution and commands — implementation plan"
status: EXECUTION_COMPLETE
target_version: "2.3.0"
spec: "docs/superpowers/specs/2026-07-28-slice-04-distribution-and-commands-design.md"
depends_on: []
---

# Distribution and Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this plugin installable through the ordinary Claude Code
marketplace route, and turn the eight invocation-carrying rows of its operating
procedure into plugin commands whose orchestrator path is expanded by the
harness rather than derived by the model.

**Architecture:** Two additions, no behaviour change. A
`.claude-plugin/marketplace.json` declares the repository as its own
marketplace so `plugin.json` becomes reachable by an installer. A `commands/`
directory holds eight thin wrappers, each running the existing orchestrator CLI
inline via `` !`…` `` so the harness expands `${CLAUDE_PLUGIN_ROOT}` and
substitutes arguments before the model sees a prompt. The operating-procedure
table in `SKILL.md` then names commands instead of bare subcommands, and a
bidirectional guard makes row↔command drift a test failure.

**Tech Stack:** Python 3.11+, pytest, ruamel.yaml, Markdown with YAML
frontmatter, JSON.

## Global Constraints

Copied from the spec; every task's requirements implicitly include these.

- **No test may invoke a real harness, a real container runtime, or the
  network.** Command files are data: tests read and assert against them, never
  execute them.
- **No new orchestrator behaviour.** Commands wrap the CLI surface that exists
  at `9750a81`. No subcommand is added and none changes meaning.
- **`python`, not `python3`**, in every command and document — matching every
  existing document in this repository. This is recorded debt, not a choice to
  revisit here.
- **Documentation uses the full `/superpowers-multiagents:<name>` form.** A
  shorter form in a document that does not work when typed is worse than a long
  one.
- **Target version is `2.3.0`.** Bumped once, in Task 5.
- **Commit trailer:** every commit ends with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Run the suite as** `python -m pytest -p no:cacheprovider` from the
  repository root.

---

## File Structure

| File | Responsibility | Task |
| :--- | :--- | :--- |
| `.claude-plugin/marketplace.json` | Declares this repo as a marketplace with one plugin entry | 1 |
| `commands/status.md` | Read the whole lifecycle state | 2 |
| `commands/activate-milestone.md` | `MILESTONE_DRAFT → MILESTONE_ACTIVE` | 2 |
| `commands/approve-spec.md` | `DRAFT_SPEC → SPEC_APPROVED` | 2 |
| `commands/approve-plan.md` | `PLAN_GENERATED → PLAN_APPROVED` | 2 |
| `commands/close-slice.md` | `EXECUTION_COMPLETE → VERIFIED_CLOSED`, plus merge and brief re-sync | 2 |
| `commands/close-milestone.md` | `MILESTONE_ACTIVE → MILESTONE_CLOSED` | 2 |
| `commands/dispatch.md` | Dispatch a configured role against a document | 2 |
| `commands/new-milestone.md` | Create a brief; the only command parsing free text | 3 |
| `skills/multiagent-orchestrator/SKILL.md` | Procedure table names commands; CLI reference unchanged | 4 |
| `README.md` | Installation via marketplace (Task 1); command set and structure (Task 5) | 1, 5 |
| `docs/architecture.md` | Records `commands/` as a component | 5 |
| `.claude-plugin/plugin.json`, `package.json` | Version `2.3.0` | 5 |
| `tests/test_docs_consistency.py` | All guards | 1–5 |

Command files are deliberately near-identical. That is not duplication to
factor out: each is a separate artifact the harness discovers by filename, and
there is no include mechanism. The guards are what keep them consistent.

---

## Task 1: Marketplace descriptor

**Files:**
- Create: `.claude-plugin/marketplace.json`
- Modify: `README.md` (the "### 1. Installation" section, currently at line 260)
- Modify: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `MARKETPLACE` — a module-level `str` in `tests/test_docs_consistency.py`
  holding the raw text of `.claude-plugin/marketplace.json`, added to the
  existing `SHIPPED_TEXT` dict under the key `.claude-plugin/marketplace.json`.
  Later tasks add their own entries to that same dict.

- [ ] **Step 1: Write the failing guards**

Append to `tests/test_docs_consistency.py`:

```python
#: Categories the official marketplace actually uses. Hardcoded because a test
#: may not reach the network; this catches a typo like "developement", not a
#: philosophical disagreement about taxonomy.
MARKETPLACE_CATEGORIES = frozenset({
    "automation", "database", "deployment", "design", "development",
    "learning", "location", "math", "monitoring", "productivity",
    "security", "testing",
})


def _marketplace():
    return json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )


def _manifest():
    return json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )


def test_marketplace_entry_matches_the_plugin_manifest():
    """Two copies of the plugin's identity must not drift apart."""
    entries = _marketplace()["plugins"]
    assert len(entries) == 1, "this repository publishes exactly one plugin"
    entry, manifest = entries[0], _manifest()
    assert entry["name"] == manifest["name"]
    assert entry["description"] == manifest["description"]


def test_marketplace_source_points_at_the_manifest_repository():
    """A source pointing somewhere else installs someone else's code."""
    entry = _marketplace()["plugins"][0]
    repository = _manifest()["repository"]
    assert repository.startswith("https://github.com/"), repository
    expected = repository[len("https://github.com/"):].removesuffix(".git")
    assert entry["source"] == {"source": "github", "repo": expected}


def test_marketplace_category_is_a_real_one():
    assert _marketplace()["plugins"][0]["category"] in MARKETPLACE_CATEGORIES


def test_marketplace_is_not_named_after_the_plugin():
    """Installation reads `<plugin>@<marketplace>`; equal names read as a typo."""
    marketplace = _marketplace()
    assert marketplace["name"] != marketplace["plugins"][0]["name"]


def test_readme_documents_the_marketplace_installation_route():
    """A source checkout is not an installation, and README used to say it was."""
    assert "marketplace" in README.lower()
    assert "plugin install" in README
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k marketplace -v`

Expected: five red. The first four are *errors*, not failures —
`FileNotFoundError` on `.claude-plugin/marketplace.json` before any assertion
runs. `test_readme_documents_the_marketplace_installation_route` is a genuine
failure on `assert "plugin install" in README`.

- [ ] **Step 3: Create the marketplace descriptor**

Create `.claude-plugin/marketplace.json`. The `description` string must be
character-for-character the one already in `.claude-plugin/plugin.json` — copy
it, do not retype it:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "ramil-zakirov-dev",
  "description": "Ramil Zakirov's Claude Code plugins",
  "owner": {
    "name": "Ramil Zakirov"
  },
  "plugins": [
    {
      "name": "superpowers-multiagents",
      "description": "Multi-agent orchestration for Superpowers workflows: a configurable N-level agent hierarchy with supervised background execution and Markdown-frontmatter lifecycle state",
      "author": {
        "name": "Ramil Zakirov"
      },
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

The entry carries no `version`: it comes from `plugin.json` at the source, and
a second copy is a second thing to forget.

- [ ] **Step 4: Rewrite the README Installation section**

In `README.md`, replace the whole block from the line `### 1. Installation`
down to (but not including) the line `**Prerequisite — the dispatched harness
needs the Superpowers skills.**` with:

````markdown
### 1. Installation

```bash
claude plugin marketplace add ramil-zakirov-dev/superpowers-multiagents
claude plugin install superpowers-multiagents@ramil-zakirov-dev
```

The repository is its own marketplace, so the two commands name the same
project: the first registers it, the second installs the plugin.

To work **on** the plugin rather than with it, clone it and install the Python
dependencies — this makes the orchestrator runnable as a program, and does not
install anything into Claude Code:

```bash
git clone https://github.com/ramil-zakirov-dev/superpowers-multiagents.git
cd superpowers-multiagents
pip install -r requirements.txt
```

````

Leave the `**Prerequisite …**` paragraph and everything after it untouched.

- [ ] **Step 5: Register the descriptor as shipped text**

In `tests/test_docs_consistency.py`, after the `PLUGIN_MANIFEST = …` line
(currently line 13), add:

```python
MARKETPLACE = (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
```

and add one entry to the existing `SHIPPED_TEXT` dict, after the
`".claude-plugin/plugin.json": PLUGIN_MANIFEST,` line:

```python
    ".claude-plugin/marketplace.json": MARKETPLACE,
```

This puts the descriptor under the existing placeholder guard.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all pass. 308 before this slice + 5 added here = **313**. If the
count differs, a test was silently lost or duplicated — investigate before
committing.

- [ ] **Step 7: Commit**

```bash
git add .claude-plugin/marketplace.json README.md tests/test_docs_consistency.py
git commit -m "feat(marketplace): publish the repository as its own marketplace

Claude Code installs plugins through marketplaces; this repository shipped no
marketplace.json, so a valid plugin.json was unreachable by any installer and
README's Installation section documented a source checkout instead. Guards pin
the entry's name, description and source to plugin.json so the two copies of
the plugin's identity cannot drift.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: The seven uniform commands

**Files:**
- Create: `commands/status.md`, `commands/activate-milestone.md`,
  `commands/approve-spec.md`, `commands/approve-plan.md`,
  `commands/close-slice.md`, `commands/close-milestone.md`,
  `commands/dispatch.md`
- Modify: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `SHIPPED_TEXT` and the `REPO_ROOT` constant from
  `tests/test_docs_consistency.py`.
- Produces:
  - `COMMANDS_DIR: Path` — `REPO_ROOT / "commands"`.
  - `command_files() -> list[Path]` — every `*.md` in `COMMANDS_DIR`, sorted by
    name. Task 3 and Task 4 both call it.
  - `command_frontmatter(path: Path) -> dict[str, str]` — the YAML frontmatter
    of one command file as a flat `str → str` mapping.

- [ ] **Step 1: Write the failing structural guards**

Append to `tests/test_docs_consistency.py`:

```python
COMMANDS_DIR = REPO_ROOT / "commands"


def command_files():
    """Every plugin command, sorted so failures name files in a stable order."""
    return sorted(COMMANDS_DIR.glob("*.md"), key=lambda p: p.name)


def command_frontmatter(path):
    """The command's YAML frontmatter as a flat mapping.

    Deliberately hand-rolled rather than routed through scripts.frontmatter:
    that module parses *lifecycle documents* and is entitled to assume a
    `status` field. A command file has none, and a shared parser would grow a
    branch for a file kind it otherwise knows nothing about.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    _, body = text.split("---\n", 1)
    block, _, _ = body.partition("\n---\n")
    fields = {}
    for line in block.split("\n"):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"{path.name}: frontmatter line is not `key: value`: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def test_every_command_declares_a_description():
    """The description is what a person sees when choosing a command."""
    assert command_files(), "no commands were found"
    for path in command_files():
        fields = command_frontmatter(path)
        assert fields.get("description"), f"{path.name} has no description"


def test_every_command_that_takes_arguments_hints_them():
    for path in command_files():
        body = path.read_text(encoding="utf-8")
        takes_arguments = "$1" in body or "$ARGUMENTS" in body
        if takes_arguments:
            assert "argument-hint" in command_frontmatter(path), (
                f"{path.name} substitutes arguments but declares no argument-hint"
            )


def test_every_command_reaches_the_orchestrator_through_the_plugin_root():
    """The whole point of the command layer: nobody computes this path.

    A literal path, a `~`, or a relative traversal would reintroduce exactly
    the failure this slice removes — and would work on the author's machine.
    """
    for path in command_files():
        body = path.read_text(encoding="utf-8")
        if "orchestrator.py" not in body:
            continue
        assert '"${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py"' in body, (
            f"{path.name} runs the orchestrator by some other path"
        )
        for forbidden in ("~/", "../scripts", "C:\\", "/home/"):
            assert forbidden not in body, (
                f"{path.name} contains the non-portable path fragment {forbidden!r}"
            )


def test_every_status_a_command_sets_is_a_real_status():
    """A typo'd status must fail here, not at the gate in front of the human."""
    import re

    from scripts.config import DEFAULT_CONFIG
    from scripts.milestone import MILESTONE_STATUSES

    known = set(DEFAULT_CONFIG["state_machine"]["valid_statuses"]) | set(MILESTONE_STATUSES)
    found = set()
    for path in command_files():
        found |= set(re.findall(r"--status ([A-Z_]+)", path.read_text(encoding="utf-8")))
    assert found, "no command sets a status"
    assert found <= known, f"unknown statuses in commands/: {sorted(found - known)}"
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k command -v`

Expected: `test_every_command_declares_a_description` fails on
`assert command_files()` — the directory does not exist, so the glob is empty.
`test_every_status_a_command_sets_is_a_real_status` fails on
`assert found`. The other two pass vacuously, which is why the first two carry
an explicit non-emptiness assertion.

- [ ] **Step 3: Create the four set-status gate commands**

These four are the same file with a different status and description. Create
each in full.

`commands/approve-spec.md`:

````markdown
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
````

`commands/approve-plan.md`:

````markdown
---
description: Record the human's approval of an implementation plan
argument-hint: [path-to-plan]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status PLAN_APPROVED`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line. What happens next is governed by the
operating procedure, not by this command.

If it failed, the orchestrator refused and its message says why. Report the
reason. Do not retry with a different status, a different file, or a direct
edit of the frontmatter.
````

`commands/activate-milestone.md`:

````markdown
---
description: Approve a written milestone brief and make it active
argument-hint: [path-to-brief]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status MILESTONE_ACTIVE`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line. What happens next is governed by the
operating procedure, not by this command.

If it failed, the orchestrator refused and its message says why. The usual
reason is an unfilled PRD section: the brief cannot leave `MILESTONE_DRAFT`
until all eight are non-empty, and the refusal lists every one that is missing.
Report them. Do not retry with a different status, a different file, or a
direct edit of the frontmatter.
````

`commands/close-milestone.md`:

````markdown
---
description: Close a milestone whose tracks are all complete
argument-hint: [path-to-brief]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status MILESTONE_CLOSED`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line.

If it failed, the orchestrator refused and its message says why. The usual
reason is an open slice: every slice listed in every track must be
`VERIFIED_CLOSED` first, and the refusal names each one that is not. Report
them. Do not retry with a different status, a different file, or a direct edit
of the frontmatter.
````

- [ ] **Step 4: Create `close-slice`, which does more than set a status**

`commands/close-slice.md`:

````markdown
---
description: Close a verified slice — merges its branch and re-syncs every milestone brief
argument-hint: [path-to-plan]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status VERIFIED_CLOSED`

The transition above has already been attempted; its output is included. It
does more than set a status: it merges the slice's feature branch and refreshes
the track checkbox in every milestone brief that lists the slice.

If it succeeded, report which briefs were refreshed, if any.

If it failed, the orchestrator refused and its message says why. Two refusals
are common: `VERIFIED_CLOSED` is legal only from `EXECUTION_COMPLETE`, and it
must target the plan file rather than the design spec. Report the reason. Do
not retry with a different status, a different file, or a direct edit of the
frontmatter.
````

- [ ] **Step 5: Create `status` and `dispatch`**

`commands/status.md`:

````markdown
---
description: Show the lifecycle status of every milestone, spec and plan
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" status --dir docs/superpowers`

Summarise the state above: which documents sit at a human gate waiting on a
decision, and which are mid-flight. Do not act on any of them — this command
reads, it does not decide.
````

`commands/dispatch.md`:

````markdown
---
description: Dispatch a configured agent role against a document
argument-hint: [role] [path-to-file]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" dispatch-agent --role "$1" --file "$2"`

Configured roles: `planner`, `executor`.

The dispatch above has already been attempted; its output is included. A
successful dispatch launches a supervised background agent. The supervisor, not
this session, sets the resulting status when that agent exits.

If it failed, report the reason. Do not retry against a different file or role,
and do not set the target status by hand.
````

- [ ] **Step 6: Add the guard that gives `dispatch.md` its teeth**

The spec's guard 8 — "every `--role` value in `commands/` is a configured
role" — cannot fail as written: `dispatch.md` passes `--role "$1"`, so there is
no literal to check and the guard would pass vacuously. A guard that can only
pass reads as coverage and provides none. It is replaced by one over the role
list the command documents, which is both checkable and useful to a reader.

Append to `tests/test_docs_consistency.py`:

```python
def test_dispatch_command_lists_exactly_the_configured_roles():
    """`--role "$1"` is a substitution, so the prose list is the only checkable
    claim the file makes — and it is the only place a user learns what to pass.
    """
    from scripts.config import DEFAULT_CONFIG

    body = (COMMANDS_DIR / "dispatch.md").read_text(encoding="utf-8")
    line = next(
        (ln for ln in body.split("\n") if ln.startswith("Configured roles:")),
        None,
    )
    assert line is not None, "dispatch.md does not state which roles exist"
    listed = {token.strip(" `.") for token in line.split(":", 1)[1].split(",")}
    assert listed == set(DEFAULT_CONFIG["agents"]), (
        f"dispatch.md lists {sorted(listed)}, configured roles are "
        f"{sorted(DEFAULT_CONFIG['agents'])}"
    )
```

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all pass. 313 + 5 added here (4 in Step 1, 1 in Step 6) = **318**.

- [ ] **Step 8: Prove the path guard is load-bearing**

Temporarily edit `commands/approve-spec.md`, replacing
`"${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py"` with
`"../scripts/orchestrator.py"`.

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k plugin_root -v`

Expected: FAIL, naming `approve-spec.md`. Revert the edit and re-run to
confirm PASS. Record both outcomes in the task report.

- [ ] **Step 9: Commit**

```bash
git add commands tests/test_docs_consistency.py
git commit -m "feat(commands): wrap the procedure's transitions as plugin commands

Seven commands run the orchestrator inline through \${CLAUDE_PLUGIN_ROOT}, so
the harness expands the path and substitutes arguments before the model sees a
prompt. The model receives a result, not an invocation to construct — which is
the one step of the lifecycle whose correctness previously rested on the model
performing a relative traversal correctly.

The spec's --role guard is replaced: dispatch.md passes --role \"\$1\", so
there is no literal to check and the guard could only pass. The documented role
list is guarded instead, which is both checkable and what a reader needs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: The `new-milestone` command

**Files:**
- Create: `commands/new-milestone.md`
- Modify: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `command_files()`, `command_frontmatter()`, `COMMANDS_DIR` from Task 2.
- Produces: nothing new for later tasks; Task 4 counts this file among the eight.

**Why this is its own task.** `milestone new` requires `--title`, and a title is
several words. Positional substitution splits on whitespace, and whether quoting
survives it is documented nowhere in `plugin-dev/command-development` — its
reference files give examples and state no rule. Rather than depend on
undocumented behaviour for the first command in the lifecycle, the split is
performed in bash, where the semantics are defined.

- [ ] **Step 1: Write the failing guard**

Append to `tests/test_docs_consistency.py`:

```python
def test_new_milestone_splits_its_arguments_in_bash():
    """The id/title split must not depend on undocumented quote handling.

    `$1`/`$2` splitting is documented by example only; nothing states what
    happens to a quoted multi-word argument. `$ARGUMENTS` is documented as the
    whole string, and `cut` has defined semantics, so the split happens there.
    """
    body = (COMMANDS_DIR / "new-milestone.md").read_text(encoding="utf-8")
    assert "$ARGUMENTS" in body, "new-milestone must take the whole argument string"
    assert "$1" not in body, "positional splitting would break a multi-word title"
    assert "cut -d' ' -f1" in body, "the id is the first word"
    assert "cut -d' ' -f2-" in body, "the title is everything after it"


def test_new_milestone_declares_the_tools_its_pipeline_needs():
    """It is the one command whose invocation is not a bare `python` call."""
    tools = command_frontmatter(COMMANDS_DIR / "new-milestone.md")["allowed-tools"]
    for tool in ("Bash(python:*)", "Bash(echo:*)", "Bash(cut:*)"):
        assert tool in tools, f"new-milestone does not declare {tool}"
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k new_milestone -v`

Expected: two failures, both `FileNotFoundError` on
`commands/new-milestone.md`.

- [ ] **Step 3: Create the command**

`commands/new-milestone.md`:

````markdown
---
description: Create a milestone brief in PRD form
argument-hint: [milestone-id] [title]
allowed-tools: Bash(python:*), Bash(echo:*), Bash(cut:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" milestone new --id "$(echo "$ARGUMENTS" | cut -d' ' -f1)" --title "$(echo "$ARGUMENTS" | cut -d' ' -f2-)"`

The brief above has already been created; its output is included. The id is the
first word of the arguments and the title is everything after it, so the title
needs no quoting.

The brief ships with eight empty PRD sections. It cannot reach
`MILESTONE_ACTIVE` until every one of them is filled — that check runs at the
next gate, not here.

If the creation failed, report the reason. Do not create the file by hand: the
section headings are a contract the section check reads.
````

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all pass. 318 + 2 = **320**.

- [ ] **Step 5: Commit**

```bash
git add commands/new-milestone.md tests/test_docs_consistency.py
git commit -m "feat(commands): create milestone briefs without depending on quote handling

new-milestone is the only command taking free text. Whether a quoted multi-word
argument survives positional substitution is documented nowhere, so the split
happens in bash — \$ARGUMENTS is documented as the whole string, and cut has
defined semantics. The model still composes nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Migrate the operating procedure and guard it both ways

**Files:**
- Modify: `skills/multiagent-orchestrator/SKILL.md` (the table at lines 162–174)
- Modify: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `command_files()` and `COMMANDS_DIR` from Task 2; the `SKILL`
  constant that already exists at the top of the test module.
- Produces: `procedure_action_cells() -> list[str]` — the third cell of every
  data row of the operating-procedure table, in document order.

**Deviation from the spec, deliberate.** The spec's guard 11 is negative — "no
row's action cell contains a bare orchestrator subcommand". Word-matching
subparser names is unreliable: `status` is a substring of `--status` and of
"statuses". It is replaced by a positive shape assertion — every action cell is
either a command token or a parenthesised note — which subsumes it and cannot
be defeated by substring accidents.

- [ ] **Step 1: Write the failing correspondence guards**

Append to `tests/test_docs_consistency.py`:

```python
COMMAND_PREFIX = "/superpowers-multiagents:"


def procedure_action_cells():
    """The third column of every data row of the Operating procedure table.

    The table is found by its header row rather than by line number so that
    editing the prose above it does not silently empty this list.
    """
    lines = SKILL.split("\n")
    header = "| When | Who decides | Run this |"
    assert header in lines, "the Operating procedure table header has changed"
    start = lines.index(header) + 2  # skip the header and the `| :--- |` row
    cells = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells.append([part.strip() for part in line.strip("|").split("|")][2])
    assert cells, "the Operating procedure table has no rows"
    return cells


def test_every_procedure_row_is_a_command_or_a_supervisor_note():
    """A row that is neither is a row someone must hand-assemble to run.

    Eight rows used to carry a bare subcommand: not runnable until the reader
    prepended `python` and a path the skill asks the model to derive. This
    assertion is positive on purpose — checking for the *absence* of subcommand
    names is defeated by `status` being a substring of `--status`.
    """
    for cell in procedure_action_cells():
        is_command = cell.startswith(f"`{COMMAND_PREFIX}")
        is_note = cell.startswith("(")
        assert is_command or is_note, (
            f"procedure row action is neither a command nor a note: {cell!r}"
        )


def test_every_procedure_command_exists_as_a_file():
    named = set()
    for cell in procedure_action_cells():
        if not cell.startswith(f"`{COMMAND_PREFIX}"):
            continue
        named.add(cell.strip("`").removeprefix(COMMAND_PREFIX).split()[0])
    assert named, "the procedure table names no commands"
    for name in sorted(named):
        assert (COMMANDS_DIR / f"{name}.md").exists(), (
            f"the procedure names {COMMAND_PREFIX}{name}, which has no file"
        )


def test_every_command_file_is_named_by_the_procedure():
    """The reverse direction. A command nobody is told to run is dead weight."""
    named = set()
    for cell in procedure_action_cells():
        if cell.startswith(f"`{COMMAND_PREFIX}"):
            named.add(cell.strip("`").removeprefix(COMMAND_PREFIX).split()[0])
    on_disk = {path.stem for path in command_files()}
    # `status` reads state and belongs to no transition, so no row names it.
    assert on_disk - named == {"status"}, (
        f"commands not named by any procedure row: {sorted(on_disk - named - {'status'})}"
    )
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k procedure -v`

Expected: `test_every_procedure_row_is_a_command_or_a_supervisor_note` fails on
the first row, `'`milestone new --id <id> --title "<title>"`'`.
`test_every_procedure_command_exists_as_a_file` fails on
`assert named`. `test_every_command_file_is_named_by_the_procedure` fails
because `on_disk - named` is all eight names, not `{"status"}`.

- [ ] **Step 3: Rewrite the table**

In `skills/multiagent-orchestrator/SKILL.md`, replace the eleven data rows of
the Operating procedure table with:

```markdown
| A milestone is agreed on | Human | `/superpowers-multiagents:new-milestone <id> <title>` |
| The brief is written | Human approves | `/superpowers-multiagents:activate-milestone <brief>` |
| A slice spec is drafted | Human approves | `/superpowers-multiagents:approve-spec <spec>` |
| Spec approved | Observable | `/superpowers-multiagents:dispatch planner <spec>` |
| Planner exited 0 | Observable | (the supervisor sets `PLAN_GENERATED`) |
| The plan is audited | Human approves | `/superpowers-multiagents:approve-plan <plan>` |
| Plan approved | Observable | `/superpowers-multiagents:dispatch executor <plan>` |
| Executor exited 0 | Observable | (the supervisor sets `EXECUTION_COMPLETE`) |
| The diff is audited | Human approves | `/superpowers-multiagents:close-slice <plan>` |
| A slice closed | Observable | (the same command re-syncs every brief listing it) |
| Every track is complete | Human approves | `/superpowers-multiagents:close-milestone <brief>` |
```

Leave the header row, the `| :--- |` separator, the paragraph above the table
and the `EXECUTION_COMPLETE` paragraph below it exactly as they are.

- [ ] **Step 4: Add the command index to the skill**

Immediately after the `EXECUTION_COMPLETE` paragraph that follows the table
(the one ending "before approving `VERIFIED_CLOSED`."), insert:

````markdown
### The command surface

These commands ship with the plugin and are available once it is installed.
They wrap the CLI documented above; the orchestrator path inside them is
expanded by the harness, so nothing derives it.

| Command | Effect |
| :--- | :--- |
| `/superpowers-multiagents:status` | Read the state of every milestone, spec and plan |
| `/superpowers-multiagents:new-milestone <id> <title>` | Create a milestone brief |
| `/superpowers-multiagents:activate-milestone <brief>` | `MILESTONE_DRAFT` → `MILESTONE_ACTIVE` |
| `/superpowers-multiagents:approve-spec <spec>` | `DRAFT_SPEC` → `SPEC_APPROVED` |
| `/superpowers-multiagents:approve-plan <plan>` | `PLAN_GENERATED` → `PLAN_APPROVED` |
| `/superpowers-multiagents:close-slice <plan>` | `EXECUTION_COMPLETE` → `VERIFIED_CLOSED`, merge, re-sync briefs |
| `/superpowers-multiagents:close-milestone <brief>` | `MILESTONE_ACTIVE` → `MILESTONE_CLOSED` |
| `/superpowers-multiagents:dispatch <role> <file>` | Dispatch a configured agent role |

`milestone sync`, `milestone check`, `summary`, `trigger-hook` and `sandbox`
have no commands: they are not steps of the procedure. Run them through the CLI
above.
````

- [ ] **Step 5: Confirm the pre-existing skill guards still hold**

Two existing tests interact with this edit, and both must stay green without
being touched:

- `test_every_milestone_subcommand_is_documented` requires the literal strings
  `milestone new`, `milestone sync` and `milestone check` in `SKILL.md`. The
  table no longer contains `milestone new`, but the "Milestone brief commands"
  section near the end of the file still does. **Do not delete that section.**
- `test_skill_resolves_the_orchestrator_by_an_absolute_or_anchored_path`
  requires the path-resolution paragraph at the top of `SKILL.md`. **Leave it.**
  The five CLI-only subcommands still need it.

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -v`

Expected: all pass.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all pass. 320 + 3 = **323**.

- [ ] **Step 7: Prove all three correspondence guards are load-bearing**

Slice 03 shipped two defects that a 303-green suite did not catch, both because
a test asserted something weaker than it appeared to. Each mutation below
isolates exactly one guard: change one thing, run, revert, then move on.

1. In `SKILL.md`, change the `approve-spec` row's action cell back to
   `` `set-status --file <spec> --status SPEC_APPROVED` ``. Revert after.
   Expected: `test_every_procedure_row_is_a_command_or_a_supervisor_note` FAILS.
2. In `SKILL.md`, misspell the `close-slice` row's command as
   `` `/superpowers-multiagents:close-slize <plan>` ``. Revert after.
   Expected: `test_every_procedure_command_exists_as_a_file` FAILS.
3. Delete `commands/approve-plan.md`, leaving its table row in place. Restore
   after.
   Expected: `test_every_procedure_command_exists_as_a_file` FAILS.
4. Delete only the `approve-plan` **row** from the table, leaving the file in
   place. Restore after.
   Expected: `test_every_command_file_is_named_by_the_procedure` FAILS.

Mutations 3 and 4 are the two halves of the bidirectional guard and must be run
separately — removing both sides at once is a legitimate change and correctly
stays green, which proves nothing.

Re-run the full suite and confirm it is green. Report all four observations.

- [ ] **Step 8: Commit**

```bash
git add skills/multiagent-orchestrator/SKILL.md tests/test_docs_consistency.py
git commit -m "feat(skill): make the operating procedure executable

Eight of the table's eleven rows gave a bare subcommand — not runnable until
the reader prepended python and the path the skill asks the model to derive.
The table that says what to run was the one place that did not say how. Rows
now name commands, and the correspondence is guarded in both directions.

The spec's negative guard (no bare subcommand in a cell) is replaced by a
positive shape assertion: every cell is a command token or a parenthesised
note. Word-matching subparser names is defeated by 'status' being a substring
of '--status'.

The path-resolution paragraph stays: milestone sync/check, summary,
trigger-hook and sandbox deliberately have no commands and still need it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Document the surface and cut 2.3.0

**Files:**
- Modify: `README.md` (the "📁 Repository Structure" block, and a new section)
- Modify: `docs/architecture.md` (the "## Module Structure" section)
- Modify: `.claude-plugin/plugin.json`, `package.json`
- Modify: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `command_files()` from Task 2.
- Produces: nothing; this is the last task.

- [ ] **Step 1: Write the failing guards**

Append to `tests/test_docs_consistency.py`:

```python
def test_readme_documents_every_command():
    """A shipped command a user cannot discover may as well not exist."""
    for path in command_files():
        assert f"{COMMAND_PREFIX}{path.stem}" in README, (
            f"README does not document {COMMAND_PREFIX}{path.stem}"
        )


def test_architecture_records_the_commands_directory():
    assert "commands/" in ARCHITECTURE
```

Then change the two version assertions that pin the release. In
`test_plugin_manifest_has_distribution_metadata`, replace
`assert manifest["version"] == "2.2.0"` with:

```python
    assert manifest["version"] == "2.3.0"
```

and in `test_package_json_version_matches_plugin_manifest`, replace the final
assertion with:

```python
    assert plugin["version"] == package["version"] == "2.3.0"
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -k "readme_documents_every_command or architecture_records or version or distribution_metadata" -v`

Expected: four failures — README documents no command, `ARCHITECTURE` has no
`commands/`, and both version assertions read `2.2.0`.

- [ ] **Step 3: Add the command section to README**

Insert a new section immediately before the `## 📁 Repository Structure`
heading:

````markdown
## ⌨️ Commands

Available once the plugin is installed. Each wraps the CLI shown above; the
orchestrator path inside them is expanded by the harness, so nothing derives
it.

| Command | Effect |
| :--- | :--- |
| `/superpowers-multiagents:status` | Read the state of every milestone, spec and plan |
| `/superpowers-multiagents:new-milestone <id> <title>` | Create a milestone brief |
| `/superpowers-multiagents:activate-milestone <brief>` | `MILESTONE_DRAFT` → `MILESTONE_ACTIVE` |
| `/superpowers-multiagents:approve-spec <spec>` | `DRAFT_SPEC` → `SPEC_APPROVED` |
| `/superpowers-multiagents:approve-plan <plan>` | `PLAN_GENERATED` → `PLAN_APPROVED` |
| `/superpowers-multiagents:close-slice <plan>` | `EXECUTION_COMPLETE` → `VERIFIED_CLOSED`, merge, re-sync briefs |
| `/superpowers-multiagents:close-milestone <brief>` | `MILESTONE_ACTIVE` → `MILESTONE_CLOSED` |
| `/superpowers-multiagents:dispatch <role> <file>` | Dispatch a configured agent role |

`milestone sync`, `milestone check`, `summary`, `trigger-hook` and `sandbox`
have no commands — they are not steps of the operating procedure. Use the CLI.

---
````

- [ ] **Step 4: Add `commands/` to the README structure block**

In the `## 📁 Repository Structure` code block, replace these seven lines:

```
├── .claude-plugin/
│   └── plugin.json             # Claude Code / Desktop plugin manifest
├── assets/
│   ├── banner.png              # Project banner graphic
│   ├── icon.png                # 24x24 project icon (PNG)
│   └── icon.svg                # 24x24 project icon (SVG)
├── docs/
```

with:

```
├── .claude-plugin/
│   ├── plugin.json             # Claude Code / Desktop plugin manifest
│   └── marketplace.json        # Marketplace descriptor: this repo publishes itself
├── assets/
│   ├── banner.png              # Project banner graphic
│   ├── icon.png                # 24x24 project icon (PNG)
│   └── icon.svg                # 24x24 project icon (SVG)
├── commands/                   # Slash commands over the orchestrator CLI
│   ├── status.md               # Read the whole lifecycle state
│   ├── new-milestone.md        # Create a milestone brief
│   ├── activate-milestone.md   # MILESTONE_DRAFT -> MILESTONE_ACTIVE
│   ├── approve-spec.md         # DRAFT_SPEC -> SPEC_APPROVED
│   ├── approve-plan.md         # PLAN_GENERATED -> PLAN_APPROVED
│   ├── close-slice.md          # EXECUTION_COMPLETE -> VERIFIED_CLOSED
│   ├── close-milestone.md      # MILESTONE_ACTIVE -> MILESTONE_CLOSED
│   └── dispatch.md             # Dispatch a configured agent role
├── docs/
```

Note `plugin.json` changes from `└──` to `├──`, because it is no longer the
last entry in its group.

- [ ] **Step 5: Record `commands/` in the architecture document**

`## Module Structure` in `docs/architecture.md` is a code-block tree of
`scripts/`, not a bullet list — `commands/` does not belong inside it. Add a
paragraph immediately after that block's closing fence and before the
`## Design Principles` heading:

```markdown
Beyond `scripts/`, the plugin ships `commands/`: one Markdown file per slash
command, each running a single orchestrator subcommand inline. They hold no
logic and add no behaviour. `${CLAUDE_PLUGIN_ROOT}` inside them is expanded by
the harness, so the path to the orchestrator is never derived at runtime — the
one step of the lifecycle that used to depend on a model resolving a relative
traversal correctly.
```

- [ ] **Step 6: Bump the version**

In `.claude-plugin/plugin.json`, change `"version": "2.2.0"` to
`"version": "2.3.0"`.

In `package.json`, change `"version": "2.2.0"` to `"version": "2.3.0"`.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all pass. 323 + 2 = **325**.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/architecture.md .claude-plugin/plugin.json package.json tests/test_docs_consistency.py
git commit -m "docs(commands): document the command surface, bump to 2.3.0

A shipped command a user cannot discover may as well not exist, so README and
SKILL.md both carry the index and a guard pins it to what is on disk.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## After the plan: what the implementer does not do

Three acceptance items belong to the repository owner and must not be attempted
by an implementing agent:

1. **Pushing `main`.** The marketplace descriptor names a `github` source, so
   the installed plugin is whatever `origin/main` holds. `origin/main` is at
   `ce3fbce`, fifteen commits behind. Until the push, an install produces a
   plugin that predates slice 03 — four of the eight commands would wrap
   invocations that installed orchestrator rejects.
2. **Installing the plugin** and confirming it appears in
   `~/.claude/plugins/installed_plugins.json`.
3. **Driving one transition end-to-end by command**, including
   `new-milestone` with a two-word title — the live check that `$ARGUMENTS`
   reaches bash whole, which no test can make.

Report the plan complete when the suite is green and the mutation results from
Task 2 Step 8 and Task 4 Step 7 are recorded. Do not report the slice accepted.
