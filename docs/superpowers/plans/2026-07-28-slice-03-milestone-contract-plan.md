---
slice_id: "slice-03-milestone-contract"
title: "Milestone contract — implementation plan"
status: PLAN_GENERATED
spec: "docs/superpowers/specs/2026-07-28-slice-03-milestone-contract-design.md"
---

# Milestone Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the milestone a declared, validated document kind with its own
three-state lifecycle, a PRD-form brief whose sections are gated, and track
checkboxes derived from real slice statuses.

**Architecture:** One new module, `scripts/milestone.py`, owns everything about
the second document kind: kind detection, the fixed state machine, the section
check, and the pure text transforms over the machine-owned track region.
`scripts/orchestrator.py` gains a `milestone` subcommand group and routes
`set-status`/`dispatch-agent` by kind. `scripts/dependencies.py` gains a shared
document resolver that both the dependency gate and the track sync use.

**Tech Stack:** Python 3.11, `ruamel.yaml` (round-trip), `pytest`, `argparse`.
No new dependency is added.

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include
this section.

- **No test may invoke a real harness, a real container runtime, or a network.**
  All git interaction uses throwaway repositories under the test's `tmp_path`.
- **No configuration surface.** This slice adds no key to `agents.yaml` and
  needs no migration of any existing config file.
- **The milestone state machine is fixed, not configurable.** Its statuses and
  transitions are module constants, never read from config.
- **No status name is added to `state_machine.valid_statuses`.**
- **The three milestone statuses are exactly** `MILESTONE_DRAFT`,
  `MILESTONE_ACTIVE`, `MILESTONE_CLOSED`.
- **The eight required sections are exactly, in this order:** `Problem`,
  `Users`, `Goals`, `Non-goals`, `Success metrics`,
  `Constraints & invariants`, `Track decomposition`, `Open questions`.
- **The markers are exactly** `<!-- tracks:begin -->` and `<!-- tracks:end -->`.
- **The list-item separator is exactly** `" — "` — space, U+2014 EM DASH, space.
- **All shipped text and all generated document content is English.** The
  chat language of the author does not apply to artefacts.
- **Commit messages use Conventional Commits and end with the trailer**
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Nothing outside the track markers is ever rewritten.** Inside them, only a
  list item's checkbox character and its suffix change; headings,
  `depends_on:` lines, blank lines and prose are reproduced verbatim.
- **Run tests with** `python -m pytest -p no:cacheprovider`. On this machine the
  default temp root denies `mkdir`, so add
  `--basetemp=<some writable dir>` if `WinError 5` appears.

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `scripts/milestone.py` (new) | Everything specific to the milestone kind: constants, kind detection, the fixed state machine, the section check, the template, and the pure region transforms. No IO except reading/writing one brief. |
| `scripts/dependencies.py` (modify) | A shared, priority-ordered document resolver; the kind-aware closed predicate applied to the dependency gate. |
| `scripts/utils.py` (modify) | `atomic_write_text` — a crash-safe whole-file write, needed because the sync rewrites a tracked file. |
| `scripts/orchestrator.py` (modify) | The `milestone` subparser and its handler; kind routing in `cmd_set_status` and `cmd_dispatch_agent`; the auto-sync call; track progress in `cmd_status`. |
| `tests/test_milestone.py` (new) | The pure surface of `milestone.py`. |
| `tests/test_milestone_cli.py` (new) | The three subcommands end to end. |
| `tests/test_milestone_routing.py` (new) | Kind routing through `set-status` and `dispatch-agent`, and the auto-sync. |
| `tests/test_dependencies.py` (modify) | The resolver priority rule and the kind-aware gate. |
| `tests/test_docs_consistency.py` (modify) | Guards that the docs and the code agree. |
| `skills/multiagent-orchestrator/SKILL.md`, `README.md`, `docs/configuration.md`, `docs/architecture.md` (modify) | Hierarchy, operating procedure, brief shape, second lifecycle. |

---

## Task 1: Document kind, the milestone lifecycle, and a shared resolver

**Files:**
- Create: `scripts/milestone.py`
- Modify: `scripts/dependencies.py`
- Test: `tests/test_milestone.py` (new), `tests/test_dependencies.py`

**Interfaces:**
- Consumes: `scripts.errors.ValidationError`, `scripts.frontmatter.parse_frontmatter`.
- Produces:
  - `milestone.MILESTONE_KIND: str = "milestone"`, `milestone.SLICE_KIND: str = "slice"`
  - `milestone.MILESTONE_STATUSES: list[str]`
  - `milestone.MILESTONE_TRANSITIONS: dict[str, list[str]]`
  - `milestone.document_kind(frontmatter: dict) -> str`
  - `milestone.is_closed(frontmatter: dict) -> bool`
  - `milestone.check_kind_declaration(path: Path, frontmatter: dict) -> None`
  - `milestone.machine_for(kind: str, config: dict) -> tuple[list[str], dict[str, list[str]]]`
  - `dependencies.resolve_document(dep_id: str, search_dirs: list[Path], exclude: Path) -> Path | None`

**Why the resolver changes.** `dependencies._resolve` returns every file whose
frontmatter `slice_id` matches. A slice has both a spec and a plan carrying the
same `slice_id`, so today every such dependency reports `ambiguous` and can
never be satisfied. The terminal status lands on the **plan**
(`skills/multiagent-orchestrator/SKILL.md`, "Verify & Close Slice"), so the
resolution order is `plans/` → `specs/` → `milestones/`. Two files in the *same*
priority group remain a genuine ambiguity and still raise.

- [ ] **Step 1: Write the failing tests for kind and lifecycle**

Create `tests/test_milestone.py`:

```python
from pathlib import Path

import pytest

from scripts import milestone
from scripts.errors import ValidationError


def test_a_document_without_a_kind_field_is_a_slice():
    """Back-compatibility: every document that exists today is a slice."""
    assert milestone.document_kind({}) == milestone.SLICE_KIND
    assert milestone.document_kind({"slice_id": "slice-01"}) == milestone.SLICE_KIND


def test_a_declared_milestone_is_a_milestone():
    assert milestone.document_kind({"kind": "milestone"}) == milestone.MILESTONE_KIND


def test_closed_means_the_terminal_status_of_the_documents_own_kind():
    assert milestone.is_closed({"status": "VERIFIED_CLOSED"})
    assert not milestone.is_closed({"status": "MILESTONE_CLOSED"})
    assert milestone.is_closed({"kind": "milestone", "status": "MILESTONE_CLOSED"})
    assert not milestone.is_closed({"kind": "milestone", "status": "VERIFIED_CLOSED"})


def test_a_file_in_milestones_without_the_kind_field_is_refused(tmp_path):
    """The authoring mistake the opt-in kind field invites.

    Not an inference of kind from location — a contradiction between two
    signals, which is refused rather than resolved by guessing.
    """
    path = tmp_path / "milestones" / "2026-07-28-milestone-1.md"
    path.parent.mkdir(parents=True)

    with pytest.raises(ValidationError) as excinfo:
        milestone.check_kind_declaration(path, {"title": "Intake"})

    assert "kind: milestone" in str(excinfo.value)


def test_a_declared_milestone_outside_milestones_is_accepted(tmp_path):
    """Location is a convention, never a load-bearing input."""
    path = tmp_path / "elsewhere" / "brief.md"
    path.parent.mkdir(parents=True)
    milestone.check_kind_declaration(path, {"kind": "milestone"})


def test_a_slice_outside_milestones_is_accepted(tmp_path):
    path = tmp_path / "specs" / "2026-07-28-slice-01-design.md"
    path.parent.mkdir(parents=True)
    milestone.check_kind_declaration(path, {"slice_id": "slice-01"})


def test_the_milestone_machine_has_three_states_and_no_failed():
    """No agent is dispatched against a milestone, so no exit code, no FAILED."""
    assert milestone.MILESTONE_STATUSES == [
        "MILESTONE_DRAFT", "MILESTONE_ACTIVE", "MILESTONE_CLOSED"
    ]
    assert "FAILED" not in milestone.MILESTONE_STATUSES


def test_the_milestone_machine_transitions():
    transitions = milestone.MILESTONE_TRANSITIONS
    assert transitions["MILESTONE_DRAFT"] == ["MILESTONE_ACTIVE"]
    assert sorted(transitions["MILESTONE_ACTIVE"]) == [
        "MILESTONE_CLOSED", "MILESTONE_DRAFT"
    ]
    assert transitions["MILESTONE_CLOSED"] == []


def test_machine_for_returns_the_kinds_own_vocabulary():
    """Each machine rejects the other's statuses at no extra cost."""
    config = {"state_machine": {
        "valid_statuses": ["DRAFT_SPEC", "VERIFIED_CLOSED"],
        "transitions": {"DRAFT_SPEC": ["VERIFIED_CLOSED"]},
    }}

    statuses, _ = milestone.machine_for(milestone.MILESTONE_KIND, config)
    assert "EXECUTING" not in statuses and "MILESTONE_CLOSED" in statuses

    statuses, _ = milestone.machine_for(milestone.SLICE_KIND, config)
    assert "MILESTONE_CLOSED" not in statuses and "DRAFT_SPEC" in statuses
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'scripts.milestone'`.

- [ ] **Step 3: Create `scripts/milestone.py` with the kind and lifecycle surface**

```python
"""The milestone document kind: its lifecycle, its brief, and its track region.

The orchestrator has exactly two document kinds. A slice is dispatched to an
agent, owns a git branch, and ends in `VERIFIED_CLOSED`. A milestone is never
dispatched, owns no branch, and ends in `MILESTONE_CLOSED` when a human says the
objective was met. Everything that differs between them lives here.
"""

from pathlib import Path

from scripts.errors import ValidationError

MILESTONE_KIND = "milestone"
SLICE_KIND = "slice"

#: The kind of a document that does not declare one. Every document that
#: existed before this module was written is a slice, so this default is what
#: makes the field opt-in rather than a migration.
DEFAULT_KIND = SLICE_KIND

#: Fixed, deliberately not configurable. Both transitions out of DRAFT and
#: ACTIVE are gated on hard-coded checks keyed to these exact names; a project
#: that renamed a status would silently detach its gate. The slice machine
#: already demonstrates the hazard — it is advertised as configurable while
#: `orchestrator.cmd_set_status` compares against the literal
#: "VERIFIED_CLOSED".
MILESTONE_STATUSES = ["MILESTONE_DRAFT", "MILESTONE_ACTIVE", "MILESTONE_CLOSED"]

MILESTONE_TRANSITIONS = {
    "MILESTONE_DRAFT": ["MILESTONE_ACTIVE"],
    "MILESTONE_ACTIVE": ["MILESTONE_DRAFT", "MILESTONE_CLOSED"],
    "MILESTONE_CLOSED": [],
}

#: What "closed" means for each kind. A milestone's terminal status is not
#: `VERIFIED_CLOSED`, so the dependency gate has to ask the document's kind
#: rather than compare against one string.
TERMINAL_STATUS = {
    SLICE_KIND: "VERIFIED_CLOSED",
    MILESTONE_KIND: "MILESTONE_CLOSED",
}

#: The directory name whose contents are expected to declare `kind: milestone`.
MILESTONES_DIRNAME = "milestones"


def document_kind(frontmatter: dict) -> str:
    """The declared kind, or `slice` when none is declared."""
    return frontmatter.get("kind") or DEFAULT_KIND


def is_closed(frontmatter: dict) -> bool:
    """True when the document sits at the terminal status of its own kind."""
    kind = document_kind(frontmatter)
    return frontmatter.get("status") == TERMINAL_STATUS.get(kind)


def check_kind_declaration(path: Path, frontmatter: dict) -> None:
    """Refuse a file in `milestones/` that does not declare `kind: milestone`.

    This does not infer kind from location. It detects a contradiction between
    where a file lives and what it says it is, and stops — which turns an
    otherwise silent misreading (a milestone validated against the slice state
    machine, or merged as if it owned a branch) into one accurate sentence.

    The converse is deliberately unguarded: a correctly declared milestone
    works wherever it is stored.
    """
    if Path(path).parent.name != MILESTONES_DIRNAME:
        return
    if document_kind(frontmatter) == MILESTONE_KIND:
        return
    raise ValidationError(
        f"{path} is in {MILESTONES_DIRNAME}/ but does not declare "
        f"`kind: {MILESTONE_KIND}` in its frontmatter. Add it, or move the file "
        f"if it is a slice document."
    )


def machine_for(kind: str, config: dict) -> tuple[list, dict]:
    """The (valid_statuses, transitions) pair that governs this kind."""
    if kind == MILESTONE_KIND:
        return list(MILESTONE_STATUSES), dict(MILESTONE_TRANSITIONS)
    state_machine = config.get("state_machine") or {}
    return state_machine.get("valid_statuses") or [], state_machine.get("transitions") or {}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: 8 passed.

- [ ] **Step 5: Write the failing tests for the shared resolver**

Append to `tests/test_dependencies.py`:

```python
def _write(path, slice_id, status, kind=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    kind_line = f"kind: {kind}\n" if kind else ""
    path.write_text(
        f'---\n{kind_line}slice_id: "{slice_id}"\nstatus: {status}\n---\n\n# X\n',
        encoding="utf-8",
    )


def test_a_slice_with_both_a_spec_and_a_plan_is_not_ambiguous(tmp_path):
    """The bug this rule fixes: every real slice has two files with one id.

    `depends_on: [slice-01]` matched both the spec and the plan, reported
    `ambiguous`, and could therefore never be satisfied. The terminal status
    lands on the plan, so the plan wins.
    """
    from scripts.dependencies import resolve_document

    specs, plans = tmp_path / "specs", tmp_path / "plans"
    _write(specs / "s.md", "slice-01", "VERIFIED_CLOSED")
    _write(plans / "p.md", "slice-01", "VERIFIED_CLOSED")

    resolved = resolve_document("slice-01", [specs, plans], exclude=tmp_path / "none.md")

    assert resolved is not None
    assert resolved.parent.name == "plans"


def test_two_files_in_the_same_priority_group_are_still_ambiguous(tmp_path):
    from scripts.dependencies import resolve_document
    from scripts.errors import ValidationError

    plans = tmp_path / "plans"
    _write(plans / "a.md", "slice-01", "VERIFIED_CLOSED")
    _write(plans / "b.md", "slice-01", "VERIFIED_CLOSED")

    with pytest.raises(ValidationError) as excinfo:
        resolve_document("slice-01", [plans], exclude=tmp_path / "none.md")

    assert "ambiguous" in str(excinfo.value)


def test_an_unresolvable_id_returns_none(tmp_path):
    from scripts.dependencies import resolve_document

    specs = tmp_path / "specs"
    specs.mkdir(parents=True)

    assert resolve_document("slice-99", [specs], exclude=tmp_path / "none.md") is None


def test_a_closed_milestone_dependency_is_met(tmp_path):
    """A milestone's terminal status is not VERIFIED_CLOSED.

    `dependencies` already searched `milestones/` before this slice, so
    comparing against one hard-coded string made a correctly closed milestone
    read as permanently unmet.
    """
    from scripts.dependencies import check_unmet_dependencies

    specs, milestones = tmp_path / "specs", tmp_path / "milestones"
    _write(milestones / "m.md", "milestone-1", "MILESTONE_CLOSED", kind="milestone")
    spec = specs / "dependent.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        '---\nslice_id: "slice-02"\nstatus: DRAFT_SPEC\n'
        'depends_on: ["milestone-1"]\n---\n\n# X\n',
        encoding="utf-8",
    )

    assert check_unmet_dependencies(spec, [specs, milestones]) == []


def test_an_active_milestone_dependency_is_unmet(tmp_path):
    from scripts.dependencies import check_unmet_dependencies

    specs, milestones = tmp_path / "specs", tmp_path / "milestones"
    _write(milestones / "m.md", "milestone-1", "MILESTONE_ACTIVE", kind="milestone")
    spec = specs / "dependent.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        '---\nslice_id: "slice-02"\nstatus: DRAFT_SPEC\n'
        'depends_on: ["milestone-1"]\n---\n\n# X\n',
        encoding="utf-8",
    )

    unmet = check_unmet_dependencies(spec, [specs, milestones])

    assert len(unmet) == 1 and "MILESTONE_ACTIVE" in unmet[0]
```

Add `import pytest` at the top of the file if it is not already imported.

- [ ] **Step 6: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_dependencies.py -v`
Expected: `ImportError: cannot import name 'resolve_document'` on the new tests.

- [ ] **Step 7: Rewrite the resolution half of `scripts/dependencies.py`**

Replace the `_resolve` function and the body of `check_unmet_dependencies`'s
loop. The final file reads:

```python
"""Slice dependency checking."""

from pathlib import Path

from scripts.errors import ValidationError
from scripts.frontmatter import parse_frontmatter
from scripts.milestone import is_closed

#: Directory names in the order a matching document is preferred. A slice's
#: spec and its plan carry the same `slice_id`; the terminal status lands on
#: the plan, so the plan answers "is this closed?".
DIRECTORY_PRIORITY = ("plans", "specs", "milestones")


def _candidate_dirs(spec_file: Path) -> list[Path]:
    """Directories to search: the file's own, plus its sibling specs/plans."""
    own = spec_file.parent
    dirs = [own]
    for sibling in ("specs", "plans", "milestones"):
        candidate = own.parent / sibling
        if candidate.is_dir() and candidate != own:
            dirs.append(candidate)
    return dirs


def _matches(dep_id: str, search_dirs: list[Path], exclude: Path) -> list[Path]:
    """Files matching `dep_id`, by frontmatter `slice_id` first, stem second."""
    by_slice_id: list[Path] = []
    by_stem: list[Path] = []
    for directory in search_dirs:
        if not Path(directory).is_dir():
            continue
        for candidate in sorted(Path(directory).glob("*.md")):
            if candidate.resolve() == Path(exclude).resolve():
                continue
            frontmatter = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if frontmatter.get("slice_id") == dep_id:
                by_slice_id.append(candidate)
            elif dep_id in candidate.stem:
                by_stem.append(candidate)
    return by_slice_id or by_stem


def resolve_document(dep_id: str, search_dirs: list[Path], exclude: Path) -> Path | None:
    """The single document `dep_id` names, or None when nothing matches.

    A frontmatter `slice_id` match always wins over a filename match. Among
    equally good matches, `DIRECTORY_PRIORITY` decides — that is what keeps a
    slice's spec and plan from reading as a conflict. Two matches inside the
    same priority group are a real ambiguity and raise, because silently
    picking one is how a dependency gate stops meaning anything.
    """
    matches = _matches(dep_id, search_dirs, exclude)
    if not matches:
        return None

    for group in DIRECTORY_PRIORITY:
        in_group = [m for m in matches if m.parent.name == group]
        if in_group:
            matches = in_group
            break

    if len(matches) > 1:
        names = sorted(match.name for match in matches)
        raise ValidationError(f"'{dep_id}' is ambiguous: matches {names}")
    return matches[0]


def check_unmet_dependencies(spec_file: Path, search_dirs: list[Path] | None = None) -> list:
    """List dependencies of a slice that are not yet closed.

    "Closed" is asked of the resolved document's own kind: `VERIFIED_CLOSED`
    for a slice, `MILESTONE_CLOSED` for a milestone.
    """
    spec_file = Path(spec_file)
    if not spec_file.exists():
        return []

    frontmatter = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
    depends_on = frontmatter.get("depends_on", [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not depends_on:
        return []

    dirs = search_dirs if search_dirs is not None else _candidate_dirs(spec_file)

    unmet = []
    for dep_id in depends_on:
        try:
            resolved = resolve_document(dep_id, dirs, exclude=spec_file)
        except ValidationError as exc:
            unmet.append(str(exc))
            continue

        if resolved is None:
            unmet.append(f"{dep_id} (spec not found in {[str(d) for d in dirs]})")
            continue

        dep_frontmatter = parse_frontmatter(resolved.read_text(encoding="utf-8"))
        if not is_closed(dep_frontmatter):
            unmet.append(f"{dep_id} (status: {dep_frontmatter.get('status', 'UNKNOWN')})")

    return unmet
```

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest -p no:cacheprovider -q`
Expected: all pass. Existing `test_dependencies.py` cases keep passing because
the priority rule only changes behaviour when several files match.

- [ ] **Step 9: Commit**

```bash
git add scripts/milestone.py scripts/dependencies.py tests/test_milestone.py tests/test_dependencies.py
git commit -m "feat(milestone): document kind, fixed lifecycle, priority-ordered resolver

A milestone is a second document kind, declared by `kind: milestone` and
defaulting to `slice` so nothing migrates. Its lifecycle is three states with
no FAILED — no agent is dispatched against it, so there is no exit code to
derive one from — and it is fixed rather than configurable because both of its
gates are keyed to these exact status names.

Also fixes a live defect in the dependency gate: a slice's spec and its plan
carry the same slice_id, so every depends_on matched two files, reported
'ambiguous', and could never be satisfied. Resolution is now ordered
plans -> specs -> milestones, because the terminal status lands on the plan.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: The section completeness check

**Files:**
- Modify: `scripts/milestone.py`
- Test: `tests/test_milestone.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the module itself.
- Produces:
  - `milestone.REQUIRED_SECTIONS: tuple[str, ...]`
  - `milestone.missing_sections(text: str) -> list[str]`

**Design note for the implementer.** A section's content ends at the *next
heading of any level*. Inside `## Track decomposition` the tracks are `###`
headings, so the only text that can satisfy that section is prose written
before them — which is exactly the decomposition rationale the template asks
for. This is intentional, not an accident of the parser.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone.py`:

```python
FILLED_BRIEF = """---
kind: milestone
status: MILESTONE_DRAFT
---

# Milestone 1

## Problem
Operators retype the same answer twenty times a day.

## Users
Support operators; back-office administrators.

## Goals
1. Cut manual retyping.

## Non-goals
**Not in this milestone:** billing.

## Success metrics
| Goal | How we will know |
| --- | --- |
| 1 | Median handle time drops below 4 minutes |

## Constraints & invariants
On-prem only.

## Track decomposition
Split by ownership boundary.

### track-1: Intake
- [ ] slice-01-gateway

## Open questions
Who owns the retention policy? — owner.
"""


def test_a_fully_written_brief_has_no_missing_sections():
    assert milestone.missing_sections(FILLED_BRIEF) == []


@pytest.mark.parametrize("section", milestone.REQUIRED_SECTIONS)
def test_each_required_section_is_reported_when_absent(section):
    """Every section is individually load-bearing, so test them individually."""
    lines = FILLED_BRIEF.splitlines()
    start = lines.index(f"## {section}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    without = "\n".join(lines[:start] + lines[end:])

    assert milestone.missing_sections(without) == [section]


def test_a_section_holding_only_its_template_hint_counts_as_empty():
    """The template's prompts are HTML comments precisely so this is true."""
    text = FILLED_BRIEF.replace(
        "Operators retype the same answer twenty times a day.",
        "<!-- Whose pain, and why now. Include what exists today. -->",
    )

    assert milestone.missing_sections(text) == ["Problem"]


def test_a_multi_line_html_comment_still_counts_as_empty():
    text = FILLED_BRIEF.replace(
        "Operators retype the same answer twenty times a day.",
        "<!-- Whose pain,\nand why now.\n-->",
    )

    assert milestone.missing_sections(text) == ["Problem"]


def test_every_offending_section_is_reported_in_one_run():
    """One attempt should not have to be repeated eight times."""
    text = FILLED_BRIEF.replace("Split by ownership boundary.", "")
    text = text.replace("On-prem only.", "")

    assert milestone.missing_sections(text) == [
        "Constraints & invariants", "Track decomposition"
    ]


def test_the_report_follows_the_canonical_section_order():
    assert milestone.missing_sections("# Empty\n") == list(milestone.REQUIRED_SECTIONS)


def test_heading_matching_is_exact_after_stripping_whitespace():
    """`## Non-goals ` matches; `## Non Goals` and `## non-goals` do not."""
    padded = FILLED_BRIEF.replace("## Non-goals", "##   Non-goals   ")
    assert milestone.missing_sections(padded) == []

    renamed = FILLED_BRIEF.replace("## Non-goals", "## Non Goals")
    assert milestone.missing_sections(renamed) == ["Non-goals"]

    lowered = FILLED_BRIEF.replace("## Non-goals", "## non-goals")
    assert milestone.missing_sections(lowered) == ["Non-goals"]


def test_a_level_three_heading_does_not_satisfy_its_parent_section():
    """Content under `### track-1` belongs to the track, not to the section."""
    text = FILLED_BRIEF.replace("Split by ownership boundary.", "")

    assert "Track decomposition" in milestone.missing_sections(text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: `AttributeError: module 'scripts.milestone' has no attribute 'REQUIRED_SECTIONS'`.

- [ ] **Step 3: Implement the check**

Add to `scripts/milestone.py`:

```python
import re

#: The brief's shape, in the order the template generates it. Presence is
#: enforced; order is not — the template supplies it, and enforcing it would
#: add a failure mode that buys nothing.
REQUIRED_SECTIONS = (
    "Problem",
    "Users",
    "Goals",
    "Non-goals",
    "Success metrics",
    "Constraints & invariants",
    "Track decomposition",
    "Open questions",
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def missing_sections(text: str) -> list[str]:
    """Required sections that are absent or hold nothing but template hints.

    A section is present when a level-2 heading equals its name exactly, after
    stripping surrounding whitespace. It is non-empty when at least one line
    beneath it — up to the next heading of any level — is neither blank, nor
    part of an HTML comment, nor a heading.

    This observes presence, never quality. `Success metrics` in particular can
    be satisfied by a plausible-looking sentence that measures nothing; the
    section is required because forcing the question is worth it, not because
    the check can judge the answer.
    """
    filled: set[str] = set()
    current: str | None = None
    in_comment = False

    for line in text.split("\n"):
        stripped = line.strip()

        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue

        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            current = title if level == 2 else None
            continue

        if current is None or not stripped:
            continue

        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue

        filled.add(current)

    return [section for section in REQUIRED_SECTIONS if section not in filled]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: all pass, including the eight parametrised cases.

- [ ] **Step 5: Commit**

```bash
git add scripts/milestone.py tests/test_milestone.py
git commit -m "feat(milestone): completeness check over the brief's eight sections

A section counts as filled only when it holds a line that is not blank, not a
heading, and not part of an HTML comment — which is why the template writes its
prompts as comments: an untouched section reads as empty.

Content ends at the next heading of any level, so the tracks' \`###\` headings
do not satisfy \`## Track decomposition\`. Only the decomposition rationale
does, which is the intent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: The template and `milestone new`

**Files:**
- Modify: `scripts/milestone.py`, `scripts/orchestrator.py`, `scripts/utils.py`
- Test: `tests/test_milestone.py`, `tests/test_milestone_cli.py` (new)

**Interfaces:**
- Consumes: `milestone.REQUIRED_SECTIONS`, `milestone.missing_sections`,
  `milestone.MILESTONE_STATUSES`, `utils._sanitize_id`.
- Produces:
  - `utils.atomic_write_text(path: Path, text: str) -> None`
  - `milestone.TRACKS_BEGIN: str`, `milestone.TRACKS_END: str`
  - `milestone.render_template(milestone_id: str, title: str) -> str`
  - `milestone.create(directory: Path, milestone_id: str, title: str, today: date) -> Path`
  - `orchestrator.cmd_milestone(args)` handling `args.action == "new"`

**Note on `atomic_write_text`.** `frontmatter.update_frontmatter_status` already
contains an inline tempfile+`os.replace` write. It is not refactored to use the
new helper: it works, it is covered, and rewriting a crash-safe write inside a
slice that does not need to is risk without benefit. The new helper exists
because the sync rewrites a whole tracked file and must not leave a truncated
brief behind.

- [ ] **Step 1: Write the failing tests for the template**

Append to `tests/test_milestone.py`:

```python
import datetime


def test_the_template_contains_every_required_section():
    text = milestone.render_template("milestone-1", "Intake automation")

    for section in milestone.REQUIRED_SECTIONS:
        assert f"## {section}" in text


def test_a_fresh_template_reports_every_section_as_empty():
    """The strongest statement that the hints are comments, not content.

    If a hint were ever written as prose, this test goes red — which is the
    only way to notice that the approval gate had quietly become a no-op.
    """
    text = milestone.render_template("milestone-1", "Intake automation")

    assert milestone.missing_sections(text) == list(milestone.REQUIRED_SECTIONS)


def test_the_template_declares_the_kind_and_the_draft_status():
    from scripts.frontmatter import parse_frontmatter

    data = parse_frontmatter(milestone.render_template("milestone-1", "Intake"))

    assert data["kind"] == "milestone"
    assert data["milestone_id"] == "milestone-1"
    assert data["title"] == "Intake"
    assert data["status"] == "MILESTONE_DRAFT"


def test_the_template_carries_both_track_markers():
    text = milestone.render_template("milestone-1", "Intake")

    assert milestone.TRACKS_BEGIN in text
    assert milestone.TRACKS_END in text
    assert text.index(milestone.TRACKS_BEGIN) < text.index(milestone.TRACKS_END)


def test_the_template_states_the_altitude():
    """"High-level yet thorough" is a tension an author resolves downward."""
    text = milestone.render_template("milestone-1", "Intake")

    assert "slice spec" in text


def test_create_writes_a_dated_file_and_returns_its_path(tmp_path):
    path = milestone.create(
        tmp_path, "milestone-1", "Intake", today=datetime.date(2026, 7, 28)
    )

    assert path == tmp_path / "milestones" / "2026-07-28-milestone-1.md"
    assert path.read_text(encoding="utf-8").startswith("---")


def test_create_refuses_to_overwrite(tmp_path):
    from scripts.errors import ValidationError

    milestone.create(tmp_path, "milestone-1", "Intake", today=datetime.date(2026, 7, 28))

    with pytest.raises(ValidationError) as excinfo:
        milestone.create(
            tmp_path, "milestone-1", "Other", today=datetime.date(2026, 7, 28)
        )

    assert "already exists" in str(excinfo.value)


def test_create_rejects_an_unsafe_id(tmp_path):
    from scripts.errors import ValidationError

    with pytest.raises(ValidationError):
        milestone.create(
            tmp_path, "../escape", "Intake", today=datetime.date(2026, 7, 28)
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: `AttributeError: module 'scripts.milestone' has no attribute 'render_template'`.

- [ ] **Step 3: Add the atomic write helper**

Append to `scripts/utils.py`:

```python
import tempfile


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a staged temp file in the same directory.

    A milestone brief is a tracked file that the orchestrator rewrites in
    place. A crash halfway through a plain write would leave a truncated
    document in the working tree; `os.replace` is atomic on both POSIX and
    Windows, so the file is either the old one or the new one.
    """
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8", newline=""
    ) as handle:
        handle.write(text)
        staged = handle.name
    os.replace(staged, path)
```

- [ ] **Step 4: Add the template and `create` to `scripts/milestone.py`**

```python
import datetime

from scripts.utils import _sanitize_id, atomic_write_text

TRACKS_BEGIN = "<!-- tracks:begin -->"
TRACKS_END = "<!-- tracks:end -->"

#: Separator between a track entry's slice_id and its machine-owned suffix:
#: space, EM DASH (U+2014), space. `_sanitize_id` admits only alphanumerics,
#: hyphens, underscores and dots, so a slice_id can never contain it and the
#: split is unambiguous.
SEPARATOR = " — "

_TEMPLATE = """---
kind: milestone
milestone_id: "{milestone_id}"
title: "{title}"
status: MILESTONE_DRAFT
---

# {title}

> A milestone is 1-3 months of work and 2-5 tracks. If you are describing a
> screen or an endpoint, you are in the wrong document — that belongs in a
> slice spec.

## Problem

<!-- Whose pain, and why now. Include what exists today and why it is
     insufficient. No solution here. -->

## Users

<!-- Who, in which roles. If this milestone is internal infrastructure, name
     the engineering roles it serves and say so in one line — an honest short
     answer beats invented personas. -->

## Goals

<!-- What becomes true when the milestone is met. One numbered goal per line. -->

## Non-goals

<!-- Two labelled groups.
     **Not in this milestone:** sequencing — we will, but later.
     **Rejected outright:** a stance that outlives this milestone.
     3-7 items; each names something a reasonable person would expect and we
     are deliberately not doing. -->

## Success metrics

<!-- A table with one row per goal above, columns `Goal` and
     `How we will know`. Add an `Overall` row for milestone-wide measures.
     A metric nobody could disagree about having been met is not a metric. -->

## Constraints & invariants

<!-- What must not be violated. One line each. -->

## Track decomposition

<!-- One sentence on why this decomposition and not another. Write it here,
     above the markers: the tracks below are `###` headings and do not count as
     this section's content. -->

{tracks_begin}
### track-1: <name>
depends_on: —
{tracks_end}

## Open questions

<!-- What is unresolved, each with the name of whoever decides it. -->
"""


def render_template(milestone_id: str, title: str) -> str:
    """The brief a `milestone new` writes: PRD form, every hint a comment."""
    return _TEMPLATE.format(
        milestone_id=milestone_id,
        title=title,
        tracks_begin=TRACKS_BEGIN,
        tracks_end=TRACKS_END,
    )


def create(
    directory: Path,
    milestone_id: str,
    title: str,
    today: datetime.date | None = None,
) -> Path:
    """Write a new brief under `<directory>/milestones/`, never overwriting."""
    _sanitize_id(milestone_id, "milestone_id")
    stamp = (today or datetime.date.today()).isoformat()
    target = Path(directory) / MILESTONES_DIRNAME / f"{stamp}-{milestone_id}.md"
    if target.exists():
        raise ValidationError(
            f"{target} already exists. Edit it, or choose another --id."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, render_template(milestone_id, title))
    return target
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: all pass.

- [ ] **Step 6: Add the `milestone` subparser and the `new` handler**

In `scripts/orchestrator.py`, add the import beside the existing ones:

```python
from scripts import milestone as milestone_mod
```

Add the handler above the `# CLI argument parser` banner:

```python
def cmd_milestone(args):
    """Milestone brief lifecycle: create, sync track state, check completeness.

    Unlike `sandbox`, flags come after the action in the ordinary way — that
    command's flags-first constraint exists only because `exec --` needs
    `argparse.REMAINDER`, and nothing here passes a command through.
    """
    if args.action == "new":
        base_dir = Path(args.dir) if args.dir else Path("docs/superpowers")
        try:
            path = milestone_mod.create(base_dir, args.id, args.title)
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"Created {path}")
        print("Fill every section, then run:")
        print(f"  set-status --file {path} --status MILESTONE_ACTIVE")
        return
```

Register the subparser inside `main()`, after the `sandbox` block:

```python
    # milestone
    p_milestone = subparsers.add_parser(
        "milestone", help="Milestone brief lifecycle (new / sync / check)"
    )
    milestone_actions = p_milestone.add_subparsers(dest="action", required=True)

    p_ms_new = milestone_actions.add_parser("new", help="Create a milestone brief")
    p_ms_new.add_argument("--id", required=True, help="Milestone id, e.g. milestone-1")
    p_ms_new.add_argument("--title", required=True, help="Milestone title")
    p_ms_new.add_argument(
        "--dir", default="docs/superpowers", help="Base superpowers directory"
    )
```

and add the dispatch line beside the others:

```python
    elif args.command == "milestone":
        cmd_milestone(args)
```

- [ ] **Step 7: Write the CLI test**

Create `tests/test_milestone_cli.py`:

```python
import argparse

import pytest

from scripts.orchestrator import cmd_milestone


def _args(action, **kwargs):
    base = dict(action=action, dir="", id="", title="", file="")
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_new_creates_a_brief_and_prints_the_next_command(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    created = list((tmp_path / "milestones").glob("*.md"))
    assert len(created) == 1
    out = capsys.readouterr().out
    assert "MILESTONE_ACTIVE" in out


def test_new_refuses_to_overwrite_and_exits_non_zero(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Other"))

    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().out
```

- [ ] **Step 8: Run the tests**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_cli.py -v`
Expected: 2 passed.

- [ ] **Step 9: Commit**

```bash
git add scripts/milestone.py scripts/utils.py scripts/orchestrator.py tests/test_milestone.py tests/test_milestone_cli.py
git commit -m "feat(milestone): PRD-form template and \`milestone new\`

The brief opens with an altitude statement and carries eight sections whose
prompts are HTML comments, so a fresh file reports every section as empty --
asserted directly, because a hint accidentally written as prose would turn the
approval gate into a no-op without anything going red.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: The track region — parsing and the line grammar

**Files:**
- Modify: `scripts/milestone.py`
- Test: `tests/test_milestone.py`

**Interfaces:**
- Consumes: `milestone.TRACKS_BEGIN`, `milestone.TRACKS_END`, `milestone.SEPARATOR`.
- Produces:
  - `milestone.ITEM_PATTERN: re.Pattern`
  - `milestone.parse_entry(line: str) -> tuple[str, str, str] | None` — returns
    `(indent, slice_id, checkbox)`, or `None` when the line is not a list item
    at all. Raises `ValidationError` for a line that looks like a list item but
    does not match the grammar.
  - `milestone.track_entries(text: str) -> list[str]` — every `slice_id` listed
    inside the markers, in document order.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone.py`:

```python
REGION_BRIEF = f"""# M

## Track decomposition

Split by ownership boundary.

{milestone.TRACKS_BEGIN}
### track-1: Intake
depends_on: —
- [ ] slice-01-gateway
- [x] slice-02-native-sandbox{milestone.SEPARATOR}VERIFIED_CLOSED · Native sandbox

### track-2: Billing
depends_on: track-1
- [ ] slice-04-ledger
{milestone.TRACKS_END}

## Open questions

None.
"""


def test_a_plain_line_is_not_an_entry():
    assert milestone.parse_entry("### track-1: Intake") is None
    assert milestone.parse_entry("depends_on: track-1") is None
    assert milestone.parse_entry("") is None


def test_an_entry_without_a_suffix_parses():
    assert milestone.parse_entry("- [ ] slice-01-gateway") == ("", "slice-01-gateway", " ")


def test_an_entry_with_a_suffix_parses_and_drops_it():
    """The suffix is machine-owned and regenerated, so it is never read back."""
    line = f"- [x] slice-02{milestone.SEPARATOR}VERIFIED_CLOSED · Native sandbox"

    assert milestone.parse_entry(line) == ("", "slice-02", "x")


def test_an_indented_entry_keeps_its_indentation():
    assert milestone.parse_entry("  - [ ] slice-01") == ("  ", "slice-01", " ")


def test_a_line_that_looks_like_an_entry_but_is_not_one_is_refused():
    """Never reinterpret a line the author may have meant differently."""
    from scripts.errors import ValidationError

    for line in ("- [] slice-01", "- [ ]slice-01", "- [y] slice-01", "- [ ] two words"):
        with pytest.raises(ValidationError):
            milestone.parse_entry(line)


def test_track_entries_lists_every_slice_id_in_document_order():
    assert milestone.track_entries(REGION_BRIEF) == [
        "slice-01-gateway", "slice-02-native-sandbox", "slice-04-ledger"
    ]


def test_entries_outside_the_markers_are_not_track_entries():
    text = REGION_BRIEF.replace("None.", "- [x] slice-99-unrelated")

    assert "slice-99-unrelated" not in milestone.track_entries(text)


def test_missing_markers_are_refused_with_both_names(tmp_path):
    from scripts.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        milestone.track_entries("# M\n\n## Track decomposition\n\nNothing.\n")

    message = str(excinfo.value)
    assert milestone.TRACKS_BEGIN in message and milestone.TRACKS_END in message


def test_markers_in_the_wrong_order_are_refused():
    from scripts.errors import ValidationError

    text = f"# M\n{milestone.TRACKS_END}\n- [ ] slice-01\n{milestone.TRACKS_BEGIN}\n"

    with pytest.raises(ValidationError):
        milestone.track_entries(text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: `AttributeError: module 'scripts.milestone' has no attribute 'parse_entry'`.

- [ ] **Step 3: Implement the grammar**

Add to `scripts/milestone.py`:

```python
#: `- [<box>] <slice_id>` with an optional machine-owned suffix. The slice_id
#: is `\S+` because `_sanitize_id` already constrains what a real one may
#: contain; anything with whitespace in it is a malformed entry, not an id.
ITEM_PATTERN = re.compile(
    r"^(?P<indent>\s*)- \[(?P<box>[ xX])\] (?P<slice_id>\S+)"
    rf"(?:{re.escape(SEPARATOR)}.*)?$"
)

#: Anything starting like a list item is held to ITEM_PATTERN.
_LOOKS_LIKE_ENTRY = re.compile(r"^\s*- \[")


def parse_entry(line: str) -> tuple[str, str, str] | None:
    """`(indent, slice_id, checkbox)` for a track entry, else None.

    A line that starts like a list item but does not match the grammar raises
    rather than being skipped: reinterpreting it would silently drop a slice
    from the milestone, and skipping it would hide a typo forever.
    """
    if not _LOOKS_LIKE_ENTRY.match(line):
        return None
    match = ITEM_PATTERN.match(line)
    if not match:
        raise ValidationError(
            f"malformed track entry: {line!r}. Expected "
            f"`- [ ] <slice_id>` or `- [x] <slice_id>`, optionally followed by "
            f"`{SEPARATOR}<generated text>`."
        )
    return match.group("indent"), match.group("slice_id"), match.group("box")


def _region_bounds(text: str) -> tuple[int, int]:
    """Line indices of the two markers. Raises when they are absent or crossed."""
    begin = end = -1
    for index, line in enumerate(text.split("\n")):
        if TRACKS_BEGIN in line and begin == -1:
            begin = index
        elif TRACKS_END in line and end == -1:
            end = index
    if begin == -1 or end == -1 or end < begin:
        raise ValidationError(
            f"track markers not found in the expected order. A milestone brief "
            f"must contain {TRACKS_BEGIN} followed by {TRACKS_END} inside its "
            f"'## Track decomposition' section. Regenerate with `milestone new`, "
            f"or add them by hand."
        )
    return begin, end


def track_entries(text: str) -> list[str]:
    """Every `slice_id` listed between the markers, in document order."""
    begin, end = _region_bounds(text)
    lines = text.split("\n")
    entries = []
    for line in lines[begin + 1:end]:
        parsed = parse_entry(line)
        if parsed is not None:
            entries.append(parsed[1])
    return entries
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/milestone.py tests/test_milestone.py
git commit -m "feat(milestone): track region bounds and the entry grammar

A line that starts like a list item but does not match the grammar raises
instead of being skipped: skipping would hide a typo forever, and
reinterpreting would silently drop a slice from the milestone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Rendering the region — the pure sync

**Files:**
- Modify: `scripts/milestone.py`
- Test: `tests/test_milestone.py`

**Interfaces:**
- Consumes: `milestone.parse_entry`, `milestone._region_bounds`, `milestone.SEPARATOR`.
- Produces:
  - `milestone.NOT_SPECCED: str = "not yet specced"`
  - `milestone.sync_text(text: str, resolve) -> str` where
    `resolve(slice_id: str) -> tuple[str | None, str | None]` returns
    `(status, title)`, or `(None, None)` when nothing resolves.
  - `milestone.progress(text: str, resolve) -> tuple[int, int]` — `(closed, total)`.
  - `milestone.unclosed(text: str, resolve) -> list[tuple[str, str]]` —
    `(slice_id, status)` for every entry that is not closed, `status` being
    `NOT_SPECCED` when it does not resolve.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone.py`:

```python
def _resolver(table):
    """A `resolve` built from a {slice_id: (status, title)} mapping."""
    def resolve(slice_id):
        return table.get(slice_id, (None, None))
    return resolve


TABLE = {
    "slice-01-gateway": ("DRAFT_SPEC", "Gateway intake"),
    "slice-02-native-sandbox": ("VERIFIED_CLOSED", "Native sandbox"),
}


def test_the_checkbox_is_ticked_only_for_verified_closed():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert "- [ ] slice-01-gateway" in out
    assert "- [x] slice-02-native-sandbox" in out


def test_the_suffix_is_regenerated_from_the_slice_frontmatter():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert f"slice-01-gateway{milestone.SEPARATOR}DRAFT_SPEC · Gateway intake" in out


def test_an_unresolvable_slice_is_rendered_not_specced_and_is_not_an_error():
    """A milestone must be able to name slices that do not exist yet."""
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert f"- [ ] slice-04-ledger{milestone.SEPARATOR}{milestone.NOT_SPECCED}" in out


def test_a_resolved_slice_without_a_title_renders_the_status_alone():
    table = dict(TABLE, **{"slice-04-ledger": ("PLAN_APPROVED", None)})

    out = milestone.sync_text(REGION_BRIEF, _resolver(table))

    assert f"- [ ] slice-04-ledger{milestone.SEPARATOR}PLAN_APPROVED\n" in out


def test_the_sync_is_idempotent_byte_for_byte():
    """Idempotency follows from regenerating the suffix, not from patching it."""
    once = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))
    twice = milestone.sync_text(once, _resolver(TABLE))

    assert once == twice


def test_nothing_outside_the_markers_is_touched():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    before = out.split(milestone.TRACKS_BEGIN)[0]
    after = out.split(milestone.TRACKS_END)[1]
    assert before == REGION_BRIEF.split(milestone.TRACKS_BEGIN)[0]
    assert after == REGION_BRIEF.split(milestone.TRACKS_END)[1]
    assert "## Open questions" in after


def test_headings_depends_on_lines_and_blank_lines_inside_the_region_survive():
    """Inside the markers the machine owns the checkbox and the suffix. Nothing else."""
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert "### track-1: Intake" in out
    assert "### track-2: Billing" in out
    assert "depends_on: —" in out
    assert "depends_on: track-1" in out
    assert "\n\n### track-2" in out


def test_a_trailing_newline_is_preserved():
    assert milestone.sync_text(REGION_BRIEF, _resolver(TABLE)).endswith("\n")


def test_a_malformed_entry_aborts_the_whole_sync():
    from scripts.errors import ValidationError

    text = REGION_BRIEF.replace("- [ ] slice-04-ledger", "- [] slice-04-ledger")

    with pytest.raises(ValidationError):
        milestone.sync_text(text, _resolver(TABLE))


def test_progress_counts_closed_over_total():
    assert milestone.progress(REGION_BRIEF, _resolver(TABLE)) == (1, 3)


def test_unclosed_reports_every_open_slice_with_its_status():
    assert milestone.unclosed(REGION_BRIEF, _resolver(TABLE)) == [
        ("slice-01-gateway", "DRAFT_SPEC"),
        ("slice-04-ledger", milestone.NOT_SPECCED),
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: `AttributeError: module 'scripts.milestone' has no attribute 'sync_text'`.

- [ ] **Step 3: Implement the rendering**

Add to `scripts/milestone.py`:

```python
#: Rendered for an entry naming a slice whose spec has not been written yet.
#: Not an error: a planning document must be able to name what does not exist.
NOT_SPECCED = "not yet specced"


def _render_entry(indent: str, slice_id: str, status, title) -> str:
    if status is None:
        return f"{indent}- [ ] {slice_id}{SEPARATOR}{NOT_SPECCED}"
    box = "x" if status == TERMINAL_STATUS[SLICE_KIND] else " "
    suffix = f"{status} · {title}" if title else status
    return f"{indent}- [{box}] {slice_id}{SEPARATOR}{suffix}"


def sync_text(text: str, resolve) -> str:
    """Rewrite every track entry's checkbox and suffix from live slice status.

    `resolve(slice_id)` returns `(status, title)`, or `(None, None)` when the
    id names nothing on disk. It may raise — an ambiguous id, for instance —
    and the exception propagates so that nothing is written.

    Splitting and rejoining on "\\n" round-trips exactly, including the
    trailing newline, so a file whose entries already read correctly comes back
    byte-identical.
    """
    begin, end = _region_bounds(text)
    lines = text.split("\n")

    for index in range(begin + 1, end):
        parsed = parse_entry(lines[index])
        if parsed is None:
            continue
        indent, slice_id, _box = parsed
        status, title = resolve(slice_id)
        lines[index] = _render_entry(indent, slice_id, status, title)

    return "\n".join(lines)


def progress(text: str, resolve) -> tuple[int, int]:
    """`(closed, total)` over every slice the brief's tracks list."""
    entries = track_entries(text)
    closed = sum(
        1 for slice_id in entries
        if resolve(slice_id)[0] == TERMINAL_STATUS[SLICE_KIND]
    )
    return closed, len(entries)


def unclosed(text: str, resolve) -> list[tuple[str, str]]:
    """`(slice_id, status)` for every listed slice that is not closed."""
    open_entries = []
    for slice_id in track_entries(text):
        status, _title = resolve(slice_id)
        if status == TERMINAL_STATUS[SLICE_KIND]:
            continue
        open_entries.append((slice_id, status or NOT_SPECCED))
    return open_entries
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/milestone.py tests/test_milestone.py
git commit -m "feat(milestone): render track state from live slice status

The suffix is regenerated rather than patched, so idempotency follows from
construction. Split/join on newline round-trips exactly, so a brief that is
already correct comes back byte-identical and nothing outside the markers can
move.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: `milestone sync` and `milestone check`

**Files:**
- Modify: `scripts/milestone.py`, `scripts/orchestrator.py`
- Test: `tests/test_milestone_cli.py`

**Interfaces:**
- Consumes: `dependencies.resolve_document`, `milestone.sync_text`,
  `milestone.missing_sections`, `utils.atomic_write_text`.
- Produces:
  - `milestone.slice_resolver(search_dirs: list[Path], exclude: Path) -> Callable`
  - `milestone.search_dirs_for(brief_path: Path) -> list[Path]`
  - `milestone.sync_file(path: Path) -> tuple[int, int]` — returns `progress`
  - `milestone.load(path: Path) -> tuple[dict, str]` — frontmatter and full text,
    refusing a document that is not `kind: milestone`
  - `orchestrator.cmd_milestone` handling `sync` and `check`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone_cli.py`:

```python
from scripts import milestone


def _brief(tmp_path, entries="- [ ] slice-01-demo\n"):
    """A milestone brief on disk, beside a `specs/` sibling."""
    milestones = tmp_path / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True)
    path = milestones / "2026-07-28-milestone-1.md"
    path.write_text(
        "---\nkind: milestone\nmilestone_id: \"milestone-1\"\n"
        "title: \"Intake\"\nstatus: MILESTONE_DRAFT\n---\n\n"
        "# Intake\n\n## Track decomposition\n\nBy boundary.\n\n"
        f"{milestone.TRACKS_BEGIN}\n### track-1: Intake\n{entries}"
        f"{milestone.TRACKS_END}\n\n## Open questions\n\nNone.\n",
        encoding="utf-8",
    )
    return path


def _spec(tmp_path, slice_id, status, title):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{slice_id}-design.md").write_text(
        f'---\nslice_id: "{slice_id}"\ntitle: "{title}"\nstatus: {status}\n---\n\n# X\n',
        encoding="utf-8",
    )


def test_sync_rewrites_the_checkbox_from_the_spec_on_disk(tmp_path, capsys):
    path = _brief(tmp_path)
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "Demo slice")

    cmd_milestone(_args("sync", file=str(path)))

    text = path.read_text(encoding="utf-8")
    assert "- [x] slice-01-demo" in text
    assert "Demo slice" in text
    assert "1/1" in capsys.readouterr().out


def test_sync_is_idempotent_on_disk(tmp_path):
    path = _brief(tmp_path)
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "Demo slice")

    cmd_milestone(_args("sync", file=str(path)))
    once = path.read_bytes()
    cmd_milestone(_args("sync", file=str(path)))

    assert path.read_bytes() == once


def test_sync_refuses_a_document_that_is_not_a_milestone(tmp_path, capsys):
    _spec(tmp_path, "slice-01-demo", "DRAFT_SPEC", "Demo")
    spec = tmp_path / "docs" / "superpowers" / "specs" / "slice-01-demo-design.md"

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("sync", file=str(spec)))

    assert excinfo.value.code == 1
    assert "kind: milestone" in capsys.readouterr().out


def test_sync_aborts_without_writing_when_a_slice_id_is_ambiguous(tmp_path, capsys):
    path = _brief(tmp_path)
    before = path.read_bytes()
    plans = tmp_path / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (plans / name).write_text(
            '---\nslice_id: "slice-01-demo"\nstatus: DRAFT_SPEC\n---\n\n# X\n',
            encoding="utf-8",
        )

    with pytest.raises(SystemExit):
        cmd_milestone(_args("sync", file=str(path)))

    assert path.read_bytes() == before
    assert "ambiguous" in capsys.readouterr().out


def test_check_reports_every_missing_section_and_exits_non_zero(tmp_path, capsys):
    path = _brief(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("check", file=str(path)))

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Problem" in out and "Open questions" in out


def test_check_passes_on_a_complete_brief(tmp_path, capsys):
    path = _brief(tmp_path)
    body = path.read_text(encoding="utf-8")
    for section in milestone.REQUIRED_SECTIONS:
        if f"## {section}" not in body:
            body += f"\n## {section}\n\nWritten.\n"
    body = body.replace("## Open questions\n\nNone.", "## Open questions\n\nNone yet.")
    path.write_text(body, encoding="utf-8")

    cmd_milestone(_args("check", file=str(path)))

    assert "complete" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_cli.py -v`
Expected: failures on the `sync`/`check` cases — `cmd_milestone` returns
`None` for those actions and nothing is written.

- [ ] **Step 3: Add the file-level helpers to `scripts/milestone.py`**

```python
from scripts.frontmatter import parse_frontmatter


def load(path: Path) -> tuple[dict, str]:
    """Frontmatter and full text of a brief, refusing any other kind.

    `milestone sync` and `milestone check` operate on one document kind. Left
    unchecked they would happily treat a slice spec as a brief and report its
    missing "Problem" section as a milestone defect.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    if document_kind(frontmatter) != MILESTONE_KIND:
        raise ValidationError(
            f"{path} does not declare `kind: {MILESTONE_KIND}`. "
            f"`milestone` commands operate on milestone briefs only."
        )
    return frontmatter, text


def search_dirs_for(brief_path: Path) -> list[Path]:
    """Where a brief's slice ids are resolved: its sibling specs/ and plans/.

    Derived the same way `dependencies._candidate_dirs` derives its siblings,
    so the dependency gate and the track sync cannot disagree about which
    files exist.
    """
    parent = Path(brief_path).parent
    dirs = []
    for sibling in ("plans", "specs"):
        candidate = parent.parent / sibling
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def slice_resolver(search_dirs: list[Path], exclude: Path):
    """A `resolve` over real files: `slice_id -> (status, title)`."""
    from scripts.dependencies import resolve_document

    def resolve(slice_id: str):
        found = resolve_document(slice_id, search_dirs, exclude=exclude)
        if found is None:
            return None, None
        data = parse_frontmatter(found.read_text(encoding="utf-8"))
        return data.get("status", "UNKNOWN"), data.get("title")

    return resolve


def sync_file(path: Path) -> tuple[int, int]:
    """Rewrite a brief's track state in place. Returns `(closed, total)`."""
    path = Path(path)
    _frontmatter, text = load(path)
    resolve = slice_resolver(search_dirs_for(path), exclude=path)
    updated = sync_text(text, resolve)
    if updated != text:
        atomic_write_text(path, updated)
    return progress(updated, resolve)
```

Move `from scripts.frontmatter import parse_frontmatter` to the module's import
block. The `resolve_document` import stays inside `slice_resolver`: `dependencies`
imports `milestone.is_closed`, so a module-level import would be circular.

- [ ] **Step 4: Extend `cmd_milestone`**

```python
    if args.action == "sync":
        try:
            closed, total = milestone_mod.sync_file(Path(args.file))
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"Synced {args.file} — {closed}/{total} slices closed.")
        return

    if args.action == "check":
        try:
            _frontmatter, text = milestone_mod.load(Path(args.file))
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        missing = milestone_mod.missing_sections(text)
        if not missing:
            print(f"{args.file}: complete — all required sections are filled.")
            return
        print(f"{args.file} is incomplete. Empty or missing sections:")
        for section in missing:
            print(f"   - {section}")
        sys.exit(1)
```

Register both subparsers in `main()`:

```python
    p_ms_sync = milestone_actions.add_parser("sync", help="Refresh track state")
    p_ms_sync.add_argument("--file", required=True, help="Path to the milestone brief")

    p_ms_check = milestone_actions.add_parser("check", help="Check section completeness")
    p_ms_check.add_argument("--file", required=True, help="Path to the milestone brief")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_cli.py -v`
Expected: all pass.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/milestone.py scripts/orchestrator.py tests/test_milestone_cli.py
git commit -m "feat(milestone): \`sync\` and \`check\` subcommands

Both refuse a document that is not \`kind: milestone\` rather than treating a
slice spec as a brief. An ambiguous slice_id aborts the sync with nothing
written, matching the dependency gate's refusal to guess.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Kind routing in `set-status`

**Files:**
- Modify: `scripts/orchestrator.py:85-160` (`cmd_set_status`)
- Test: `tests/test_milestone_routing.py` (new)

**Interfaces:**
- Consumes: `milestone.check_kind_declaration`, `milestone.document_kind`,
  `milestone.machine_for`, `milestone.missing_sections`, `milestone.unclosed`,
  `milestone.load`, `milestone.slice_resolver`, `milestone.search_dirs_for`,
  `frontmatter.update_frontmatter_status`.
- Produces: `cmd_set_status` routing by kind; unchanged behaviour for slices.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_milestone_routing.py`:

```python
import argparse

import pytest

from scripts import milestone
from scripts.frontmatter import parse_frontmatter
from scripts.orchestrator import cmd_set_status


def _args(file, status):
    return argparse.Namespace(file=str(file), status=status)


def _write_brief(tmp_project, status="MILESTONE_DRAFT", filled=True, entries=""):
    milestones = tmp_project / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True, exist_ok=True)
    path = milestones / "2026-07-28-milestone-1.md"
    sections = "".join(
        f"\n## {name}\n\nWritten.\n"
        for name in milestone.REQUIRED_SECTIONS
        if name != "Track decomposition"
    )
    if not filled:
        sections = sections.replace("## Problem\n\nWritten.", "## Problem\n")
    path.write_text(
        f"---\nkind: milestone\nmilestone_id: \"milestone-1\"\n"
        f"title: \"Intake\"\nstatus: {status}\n---\n\n# Intake\n"
        f"{sections}"
        f"\n## Track decomposition\n\nBy boundary.\n\n"
        f"{milestone.TRACKS_BEGIN}\n### track-1: Intake\n{entries}"
        f"{milestone.TRACKS_END}\n",
        encoding="utf-8",
    )
    return path


def _status_of(path):
    return parse_frontmatter(path.read_text(encoding="utf-8"))["status"]


def test_a_milestone_is_validated_against_its_own_machine(tmp_project):
    """`EXECUTING` describes a dispatched agent. A milestone has none."""
    path = _write_brief(tmp_project)

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "EXECUTING"))

    assert _status_of(path) == "MILESTONE_DRAFT"


def test_a_slice_cannot_take_a_milestone_status(tmp_project, demo_spec):
    with pytest.raises(SystemExit):
        cmd_set_status(_args(demo_spec, "MILESTONE_CLOSED"))

    assert _status_of(demo_spec) == "SPEC_APPROVED"


def test_activating_a_brief_with_an_empty_section_is_refused(tmp_project, capsys):
    path = _write_brief(tmp_project, filled=False)

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "MILESTONE_ACTIVE"))

    assert _status_of(path) == "MILESTONE_DRAFT"
    assert "Problem" in capsys.readouterr().out


def test_activating_a_complete_brief_succeeds(tmp_project):
    path = _write_brief(tmp_project)

    cmd_set_status(_args(path, "MILESTONE_ACTIVE"))

    assert _status_of(path) == "MILESTONE_ACTIVE"


def test_closing_with_an_unclosed_slice_is_refused(tmp_project, capsys):
    path = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n"
    )

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    assert _status_of(path) == "MILESTONE_ACTIVE"
    out = capsys.readouterr().out
    assert "slice-01-demo" in out and "SPEC_APPROVED" in out


def test_closing_succeeds_once_every_listed_slice_is_closed(tmp_project, demo_spec):
    demo_spec.write_text(
        demo_spec.read_text(encoding="utf-8").replace(
            "status: SPEC_APPROVED", "status: VERIFIED_CLOSED"
        ),
        encoding="utf-8",
    )
    path = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n"
    )

    cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    assert _status_of(path) == "MILESTONE_CLOSED"


def test_closing_a_milestone_performs_no_git_operation(tmp_project, demo_spec):
    """A milestone owns no branch. Nothing here may reach merge_and_cleanup."""
    demo_spec.write_text(
        demo_spec.read_text(encoding="utf-8").replace(
            "status: SPEC_APPROVED", "status: VERIFIED_CLOSED"
        ),
        encoding="utf-8",
    )
    path = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n"
    )

    cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    # There is no `feat/2026-07-28-milestone-1` branch in this repository, so a
    # regression that reached merge_and_cleanup_worktree would fail the merge,
    # try to record MERGE_CONFLICT — which the milestone machine does not have —
    # and exit non-zero. Completing at all is the assertion; the status proves
    # the write happened, and the absent worktree proves nothing was cleaned up.
    assert _status_of(path) == "MILESTONE_CLOSED"
    assert not (tmp_project / ".worktrees").exists()


def test_a_file_in_milestones_without_the_kind_field_is_refused(tmp_project, capsys):
    milestones = tmp_project / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True, exist_ok=True)
    path = milestones / "2026-07-28-milestone-2.md"
    path.write_text('---\ntitle: "Oops"\nstatus: DRAFT_SPEC\n---\n\n# Oops\n', encoding="utf-8")

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "SPEC_APPROVED"))

    assert "kind: milestone" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: multiple failures — a milestone currently accepts `EXECUTING`, and
`MILESTONE_ACTIVE` is rejected as an unknown status.

- [ ] **Step 3: Route `cmd_set_status` by kind**

Replace the opening of `cmd_set_status` (everything from `filepath = ...` down
to the `if args.status != "VERIFIED_CLOSED":` block) with:

```python
    filepath = Path(args.file).resolve()
    project_root = find_project_root(filepath)

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    frontmatter = parse_frontmatter(filepath.read_text(encoding="utf-8"))
    try:
        milestone_mod.check_kind_declaration(filepath, frontmatter)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    kind = milestone_mod.document_kind(frontmatter)
    valid_statuses, transitions = milestone_mod.machine_for(kind, config)

    if kind == milestone_mod.MILESTONE_KIND:
        _set_milestone_status(filepath, args.status, valid_statuses, transitions)
        return

    if args.status != "VERIFIED_CLOSED":
        if not update_frontmatter_status(filepath, args.status, valid_statuses, transitions):
            sys.exit(1)
        return

    slice_id = frontmatter.get("slice_id", filepath.stem)
    current_status = frontmatter.get("status", "UNKNOWN")
```

The rest of the slice path — the pre-merge legality check, the merge, the
`MERGE_CONFLICT` handling, the hook and the teardown — stays exactly as it is.

Add the milestone handler above `cmd_set_status`:

```python
def _set_milestone_status(filepath, new_status, valid_statuses, transitions):
    """A milestone's transitions. No branch, no worktree, no sandbox.

    Both gates run before the status write, so a refused transition leaves the
    document exactly as it was.
    """
    text = filepath.read_text(encoding="utf-8")

    if new_status == "MILESTONE_ACTIVE":
        missing = milestone_mod.missing_sections(text)
        if missing:
            print(f"Error: {filepath.name} cannot be approved while sections are empty:")
            for section in missing:
                print(f"   - {section}")
            sys.exit(1)

    if new_status == "MILESTONE_CLOSED":
        resolve = milestone_mod.slice_resolver(
            milestone_mod.search_dirs_for(filepath), exclude=filepath
        )
        try:
            open_slices = milestone_mod.unclosed(text, resolve)
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if open_slices:
            print(f"Error: {filepath.name} cannot be closed; these slices are open:")
            for slice_id, status in open_slices:
                print(f"   - {slice_id} ({status})")
            sys.exit(1)

    if not update_frontmatter_status(filepath, new_status, valid_statuses, transitions):
        sys.exit(1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: all pass.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -p no:cacheprovider -q`
Expected: all pass — `test_set_status.py` is untouched because the slice path
is unchanged.

- [ ] **Step 6: Commit**

```bash
git add scripts/orchestrator.py tests/test_milestone_routing.py
git commit -m "feat(milestone): route set-status by document kind

A milestone is validated against its own three-state machine and never reaches
merge_and_cleanup_worktree — before this, VERIFIED_CLOSED on a brief tried to
merge a branch named after the file stem. Both gates run before the status
write, so a refusal leaves the document untouched.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Auto-sync when a slice closes

**Files:**
- Modify: `scripts/milestone.py`, `scripts/orchestrator.py`
- Test: `tests/test_milestone_routing.py`

**Interfaces:**
- Consumes: `milestone.sync_file`, `milestone.track_entries`, `milestone.load`.
- Produces: `milestone.briefs_listing(slice_id: str, project_root: Path) -> list[Path]`
  and a call to it at the end of the slice `VERIFIED_CLOSED` path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone_routing.py`:

Add these helpers near the top of the file first — the fixture is the part
that gets this test wrong:

```python
import subprocess

from scripts.git_ops import create_git_worktree


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _closeable_slice(tmp_project, slice_id="slice-01-demo", title="Demo"):
    """A plan at EXECUTION_COMPLETE with a real, mergeable branch behind it.

    `set-status --status VERIFIED_CLOSED` runs an actual
    `git merge feat/<slice_id>` (`git_ops.merge_and_cleanup_worktree`). Without
    the branch, the merge fails, the slice lands in MERGE_CONFLICT, and the
    command exits *before* the auto-sync ever runs — so a test written without
    this fixture goes red for a reason that has nothing to do with what it
    tests, and the obvious "fix" is to move the auto-sync earlier, which would
    be wrong. Mirrors the setup in `tests/test_set_status.py`.

    Call this AFTER writing the brief: it commits the whole tree, and the merge
    refuses to run against a dirty one.
    """
    plans = tmp_project / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan_file = plans / f"2026-07-28-{slice_id}-plan.md"
    plan_file.write_text(
        f'---\nslice_id: "{slice_id}"\ntitle: "{title}"\n'
        f"status: EXECUTION_COMPLETE\n---\n\n# Plan\n",
        encoding="utf-8",
    )
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "fixture")

    worktree = create_git_worktree(slice_id, tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")
    return plan_file
```

Then the tests:

```python
def test_closing_a_slice_ticks_it_in_every_brief_that_lists_it(tmp_project):
    """Closing a slice and updating the milestone are one command.

    A checkbox therefore cannot go stale, and nobody has to remember a step.
    """
    brief = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n"
    )
    plan_file = _closeable_slice(tmp_project)

    cmd_set_status(_args(plan_file, "VERIFIED_CLOSED"))

    assert _status_of(plan_file) == "VERIFIED_CLOSED", "the merge must have succeeded"
    assert "- [x] slice-01-demo" in brief.read_text(encoding="utf-8")


def test_a_brief_that_does_not_list_the_slice_is_untouched(tmp_project):
    brief = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-99-other\n"
    )
    plan_file = _closeable_slice(tmp_project)
    before = brief.read_bytes()

    cmd_set_status(_args(plan_file, "VERIFIED_CLOSED"))

    assert _status_of(plan_file) == "VERIFIED_CLOSED"
    assert brief.read_bytes() == before


def test_a_failing_auto_sync_warns_but_does_not_reopen_the_slice(
    tmp_project, monkeypatch, capsys
):
    """The close was already recorded. A later step must not unrecord it."""
    from scripts import milestone as milestone_module
    from scripts.errors import ValidationError

    _write_brief(tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n")
    plan_file = _closeable_slice(tmp_project)

    def boom(_path):
        raise ValidationError("markers missing")

    monkeypatch.setattr(milestone_module, "sync_file", boom)

    cmd_set_status(_args(plan_file, "VERIFIED_CLOSED"))

    assert _status_of(plan_file) == "VERIFIED_CLOSED"
    assert "Warning" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: the checkbox is still `- [ ]` after the slice closed.

- [ ] **Step 3: Implement brief discovery**

Add to `scripts/milestone.py`:

```python
def briefs_listing(slice_id: str, start_path: Path) -> list[Path]:
    """Milestone briefs whose track region lists `slice_id`.

    Resolved from the closing document's own location the same way
    `search_dirs_for` resolves siblings, so a project laid out differently
    still finds its milestones. A brief that cannot be read — wrong kind,
    missing markers — is skipped rather than raising: discovery must not be
    able to fail a slice's closure.
    """
    milestones_dir = Path(start_path).parent.parent / MILESTONES_DIRNAME
    if not milestones_dir.is_dir():
        return []

    listing = []
    for candidate in sorted(milestones_dir.glob("*.md")):
        try:
            _frontmatter, text = load(candidate)
            entries = track_entries(text)
        except (OSError, ValidationError):
            continue
        if slice_id in entries:
            listing.append(candidate)
    return listing
```

- [ ] **Step 4: Call it after a slice closes**

In `cmd_set_status`, immediately after the successful
`update_frontmatter_status(filepath, "VERIFIED_CLOSED", ...)` call and before
the `on_slice_verified_closed` hook, add:

```python
    # Closing a slice and refreshing the milestones that list it are one
    # command, so a checkbox cannot go stale. A sync failure is a warning: the
    # slice's outcome is already recorded, and a later step must not overturn
    # it -- the same rule the hook and the sandbox teardown below follow.
    for brief in milestone_mod.briefs_listing(slice_id, filepath):
        try:
            milestone_mod.sync_file(brief)
            print(f"Refreshed {brief.name}.")
        except OrchestratorError as exc:
            print(f"Warning: could not refresh {brief.name}: {exc}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/milestone.py scripts/orchestrator.py tests/test_milestone_routing.py
git commit -m "feat(milestone): refresh briefs when a slice closes

Closing a slice and updating every milestone that lists it are one command, so
a checkbox cannot go stale and nobody has to remember a step. A sync failure is
a warning only: the close is already recorded and must not be overturned.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 9: `dispatch-agent` refuses a milestone

**Files:**
- Modify: `scripts/orchestrator.py:196-230` (`cmd_dispatch_agent`)
- Test: `tests/test_milestone_routing.py`

**Interfaces:**
- Consumes: `milestone.check_kind_declaration`, `milestone.document_kind`.
- Produces: no new API — a refusal placed before the lock is taken.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_milestone_routing.py`:

```python
def test_dispatching_an_agent_against_a_milestone_is_refused(tmp_project, capsys):
    """No role operates on a brief. Before this, a milestone at SPEC_APPROVED
    passed the state gate and a worktree named after the file was created."""
    from scripts.orchestrator import cmd_dispatch_agent

    brief = _write_brief(tmp_project, status="MILESTONE_ACTIVE")

    with pytest.raises(SystemExit) as excinfo:
        cmd_dispatch_agent(
            argparse.Namespace(role="planner", file=str(brief), model=None)
        )

    assert excinfo.value.code == 1
    assert "milestone" in capsys.readouterr().out.lower()


def test_a_refused_dispatch_leaves_no_lock_and_no_worktree(tmp_project):
    from scripts.orchestrator import cmd_dispatch_agent

    brief = _write_brief(tmp_project, status="MILESTONE_ACTIVE")

    with pytest.raises(SystemExit):
        cmd_dispatch_agent(
            argparse.Namespace(role="planner", file=str(brief), model=None)
        )

    assert not (tmp_project / ".worktrees").exists()
    locks = tmp_project / ".superpowers" / "locks"
    assert not locks.exists() or not list(locks.glob("*.lock"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: no `SystemExit` — the dispatch proceeds.

- [ ] **Step 3: Add the refusal**

In `cmd_dispatch_agent`, replace the block that reads the frontmatter
(currently `orchestrator.py:221-223`) with:

```python
    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))

    try:
        milestone_mod.check_kind_declaration(target_file, frontmatter)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if milestone_mod.document_kind(frontmatter) == milestone_mod.MILESTONE_KIND:
        print(
            f"[Kind Gate] Cannot dispatch {role} for {target_file.name}: it is a "
            f"milestone brief. No agent role operates on a milestone — dispatch "
            f"against the slice spec or plan the brief lists."
        )
        sys.exit(1)

    slice_id = frontmatter.get("slice_id", target_file.stem)
    current_status = frontmatter.get("status", "UNKNOWN")
```

This sits before step 3 of the documented dispatch ordering (the lock), so a
refusal leaves no artefact behind.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_routing.py -v`
Expected: all pass.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/orchestrator.py tests/test_milestone_routing.py
git commit -m "feat(milestone): dispatch-agent refuses a milestone brief

The only gate was allowed_statuses, so a brief sitting at a status a role
accepts would have had the planner dispatched against it and a worktree created
under the milestone's file name. The refusal runs before the lock, so nothing
is left behind.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 10: Track progress in `status`

**Files:**
- Modify: `scripts/orchestrator.py:57-83` (`cmd_status`)
- Test: `tests/test_milestone_cli.py`

**Interfaces:**
- Consumes: `milestone.document_kind`, `milestone.progress`,
  `milestone.slice_resolver`, `milestone.search_dirs_for`.
- Produces: no new API.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_milestone_cli.py`:

```python
def test_status_reports_track_progress_for_milestones(tmp_path, capsys):
    from scripts.orchestrator import cmd_status

    path = _brief(tmp_path, entries="- [ ] slice-01-demo\n- [ ] slice-02-demo\n")
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "One")
    _spec(tmp_path, "slice-02-demo", "DRAFT_SPEC", "Two")

    cmd_status(argparse.Namespace(dir=str(tmp_path / "docs" / "superpowers")))

    assert "(1/2 slices closed)" in capsys.readouterr().out


def test_status_survives_a_milestone_whose_markers_are_missing(tmp_path, capsys):
    """A malformed brief must not take down the whole report."""
    from scripts.orchestrator import cmd_status

    milestones = tmp_path / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True)
    (milestones / "broken.md").write_text(
        '---\nkind: milestone\nstatus: MILESTONE_DRAFT\ntitle: "Broken"\n---\n\n# B\n',
        encoding="utf-8",
    )

    cmd_status(argparse.Namespace(dir=str(tmp_path / "docs" / "superpowers")))

    assert "Broken" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_cli.py -v`
Expected: `AssertionError` — no progress suffix is printed.

- [ ] **Step 3: Add the progress suffix**

Replace the print loop inside `cmd_status`:

```python
        for filepath in md_files:
            text = filepath.read_text(encoding="utf-8")
            data = parse_frontmatter(text)
            status = data.get("status", "UNKNOWN")
            title = data.get("title", filepath.stem)
            suffix = ""
            if milestone_mod.document_kind(data) == milestone_mod.MILESTONE_KIND:
                try:
                    resolve = milestone_mod.slice_resolver(
                        milestone_mod.search_dirs_for(filepath), exclude=filepath
                    )
                    closed, total = milestone_mod.progress(text, resolve)
                    suffix = f" ({closed}/{total} slices closed)"
                except OrchestratorError:
                    # A brief without markers is still worth listing; a broken
                    # one must not take the whole report down with it.
                    suffix = " (track state unavailable)"
            print(f"  [{status:<18}] {filepath.name} - {title}{suffix}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider tests/test_milestone_cli.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/orchestrator.py tests/test_milestone_cli.py
git commit -m "feat(milestone): show track progress in the status report

A brief whose markers are missing degrades to '(track state unavailable)'
rather than taking the whole report down.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 11: Documentation and the consistency guards

**Files:**
- Modify: `skills/multiagent-orchestrator/SKILL.md`, `README.md`,
  `docs/configuration.md`, `docs/architecture.md`,
  `.claude-plugin/plugin.json`, `package.json`
- Test: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `milestone.MILESTONE_STATUSES`, `milestone.REQUIRED_SECTIONS`,
  `milestone.TRACKS_BEGIN`, `milestone.TRACKS_END`.
- Produces: no new API.

- [ ] **Step 1: Write the failing guards**

Append to `tests/test_docs_consistency.py`:

```python
def test_every_milestone_status_is_documented():
    from scripts.milestone import MILESTONE_STATUSES

    documented = README + CONFIGURATION + ARCHITECTURE + SKILL
    for status in MILESTONE_STATUSES:
        assert status in documented, f"milestone status '{status}' is undocumented"


def test_every_milestone_subcommand_is_documented():
    for action in ("milestone new", "milestone sync", "milestone check"):
        assert action in SKILL, f"'{action}' is not shown in SKILL.md"


def test_the_documented_required_sections_match_the_code():
    """A brief's shape is a contract; two copies of it must not drift."""
    from scripts.milestone import REQUIRED_SECTIONS

    for section in REQUIRED_SECTIONS:
        assert section in CONFIGURATION, (
            f"required section '{section}' is enforced by the code but appears "
            f"nowhere in docs/configuration.md"
        )


def test_the_track_markers_are_documented_verbatim():
    from scripts.milestone import TRACKS_BEGIN, TRACKS_END

    assert TRACKS_BEGIN in CONFIGURATION and TRACKS_END in CONFIGURATION


def test_the_sandbox_flags_first_warning_is_not_copied_onto_milestone():
    """That constraint exists only because `sandbox exec --` needs REMAINDER."""
    for line in SKILL.split("\n"):
        if "must precede the action" in line.lower():
            assert "sandbox" in line.lower(), (
                "the flags-first warning was generalised beyond sandbox; "
                "milestone subcommands take flags after the action"
            )


def test_the_milestone_lifecycle_is_documented_as_fixed():
    assert "not configurable" in ARCHITECTURE.lower()


def test_the_operating_procedure_states_who_decides():
    """The distinction the section exists for: the human decides, never types."""
    assert "Operating procedure" in SKILL
    assert "decides" in SKILL


def test_package_json_version_matches_plugin_manifest():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert plugin["version"] == package["version"] == "2.2.0"
```

Delete the two older assertions that pin the version to `2.1.0`:
`test_plugin_manifest_has_distribution_metadata`'s last line becomes
`assert manifest["version"] == "2.2.0"`, and the previous
`test_package_json_version_matches_plugin_manifest` is replaced by the one
above.

- [ ] **Step 2: Run the guards to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/test_docs_consistency.py -v`
Expected: failures on the milestone statuses, the subcommands, the sections,
the markers, the lifecycle sentence, the operating procedure, and the version.

- [ ] **Step 3: Bump the version**

Set `"version": "2.2.0"` in both `.claude-plugin/plugin.json` and
`package.json`.

- [ ] **Step 4: Document the second lifecycle in `docs/architecture.md`**

Add `scripts/milestone.py` to the module tree listing, and append a section
after "## State Machine":

```markdown
## The milestone lifecycle

A milestone brief is the orchestrator's second document kind, declared by
`kind: milestone` in its frontmatter. A document that declares no kind is a
slice, which is what makes the field opt-in rather than a migration.

```
MILESTONE_DRAFT ⇄ MILESTONE_ACTIVE → MILESTONE_CLOSED
```

| Transition | Gate |
| :--- | :--- |
| `DRAFT → ACTIVE` | Every required section present and non-empty |
| `ACTIVE → DRAFT` | None |
| `ACTIVE → CLOSED` | Every slice listed in every track is `VERIFIED_CLOSED` |

There is no `FAILED`: no agent is dispatched against a milestone, so there is no
exit code to derive a terminal status from. There is no merge either — a
milestone owns no branch, and `set-status` never reaches
`merge_and_cleanup_worktree` for one.

**This state machine is fixed and deliberately not configurable.** Both of its
gates are keyed to these exact status names, so a project that renamed
`MILESTONE_CLOSED` would silently detach the gate from the transition. The slice
machine demonstrates the hazard it avoids: it is advertised as configurable
while `cmd_set_status` compares against the literal `"VERIFIED_CLOSED"`.

`MILESTONE_CLOSED` is always a human decision. The gate refuses a premature
close; it never performs one. "Every slice shipped" and "the objective was met"
are different claims.

### Track state is derived, never hand-ticked

A track lists its slices by `slice_id` inside a machine-owned region delimited
by `<!-- tracks:begin -->` and `<!-- tracks:end -->`. The brief owns membership;
the slice files own status. That direction is required by what a milestone is —
a planning document must be able to name slices that do not exist yet, which
the opposite arrangement (slices declaring a `track_id`) cannot express.

Closing a slice re-syncs every brief that lists it in the same command, so a
checkbox cannot go stale. A failure to re-sync is reported as a warning and does
not overturn the close, for the same reason a failing `on_slice_verified_closed`
hook does not: the outcome was already recorded.
```

- [ ] **Step 5: Document the brief's shape in `docs/configuration.md`**

Append a section after "Prompt templates and their skill dependency":

```markdown
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
```

- [ ] **Step 6: Add the operating procedure to `SKILL.md`**

Update the hierarchy section to name the brief and its statuses, then append:

```markdown
## Operating procedure

These are obligations, not options. **The human always decides, and never
types.** Transitions whose trigger is an observable fact are run without being
asked. Transitions that exist because a human approved something are run
*immediately after* the approval, never in place of it — automating the
keystroke is the goal; automating the judgement would dissolve the gate.

| When | Who decides | Run this |
| :--- | :--- | :--- |
| A milestone is agreed on | Human | `milestone new --id <id> --title "<title>"` |
| The brief is written | Human approves | `set-status --file <brief> --status MILESTONE_ACTIVE` |
| A slice spec is drafted | Human approves | `set-status --file <spec> --status SPEC_APPROVED` |
| Spec approved | Observable | `dispatch-agent --role planner --file <spec>` |
| Planner exited 0 | Observable | (the supervisor sets `PLAN_GENERATED`) |
| The plan is audited | Human approves | `set-status --file <plan> --status PLAN_APPROVED` |
| Plan approved | Observable | `dispatch-agent --role executor --file <plan>` |
| Executor exited 0 | Observable | (the supervisor sets `EXECUTION_COMPLETE`) |
| The diff is audited | Human approves | `set-status --file <plan> --status VERIFIED_CLOSED` |
| A slice closed | Observable | (the same command re-syncs every brief listing it) |
| Every track is complete | Human approves | `set-status --file <brief> --status MILESTONE_CLOSED` |

`EXECUTION_COMPLETE` means the executor's process ended cleanly, not that the
plan is finished — read the plan's unchecked task boxes before approving
`VERIFIED_CLOSED`.

### Milestone brief commands

Flags come after the action here, unlike `sandbox`.

```bash
python "<orchestrator>" milestone new --id milestone-1 --title "Intake automation"
python "<orchestrator>" milestone sync --file docs/superpowers/milestones/2026-07-28-milestone-1.md
python "<orchestrator>" milestone check --file docs/superpowers/milestones/2026-07-28-milestone-1.md
```

`sync` is for repair and backfill: closing a slice already refreshes every brief
that lists it.
```

- [ ] **Step 7: Update `README.md`**

Insert this immediately after the slice state-machine table (the row ending
`| \`VERIFIED_CLOSED\` | **Opus 5 Gate** | ... |`), before the `---` that
follows it:

```markdown
### Milestone lifecycle

A milestone brief is a second document kind, declared by `kind: milestone`. A
document that declares none is a slice, so nothing existing changes.

| State | Responsible | Action / Gate |
| :--- | :--- | :--- |
| `MILESTONE_DRAFT` | **Agent 1** | Writing the brief. |
| `MILESTONE_ACTIVE` | **Human Gate** | Approved — refused while any required section is empty. |
| `MILESTONE_CLOSED` | **Human Gate** | The objective was met — refused while any listed slice is open. |

No agent is ever dispatched against a brief, so there is no `FAILED` here and no
branch to merge. Track checkboxes are derived from the real statuses of the
slices a track lists; closing a slice refreshes them in the same command. See
[docs/configuration.md](docs/configuration.md#milestone-briefs).
```

And in the Quickstart, after the "Check Workflow Status" block, add:

```markdown
### 3. Start a Milestone

```bash
python "/abs/path/to/plugin/scripts/orchestrator.py" milestone new --id milestone-1 --title "Intake automation"
```

Fill every section of the generated brief, then approve it:

```bash
python "/abs/path/to/plugin/scripts/orchestrator.py" set-status --file docs/superpowers/milestones/<file>.md --status MILESTONE_ACTIVE
```
```

Renumber the Quickstart headings that follow.

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add README.md docs/architecture.md docs/configuration.md skills/multiagent-orchestrator/SKILL.md .claude-plugin/plugin.json package.json tests/test_docs_consistency.py
git commit -m "docs(milestone): brief shape, second lifecycle, operating procedure

SKILL.md gains an Operating procedure written as obligations: the human always
decides and never types. Guards keep the documented section list, the markers
and the milestone statuses tied to the code, so the two copies cannot drift.

Bumps to 2.2.0.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Acceptance

Beyond a green suite, three things must be observed by hand before this slice
is closed. The first is a deployment precondition without which the whole
instruction layer is dead text.

1. **Install the plugin in Claude Desktop** and confirm
   `multiagent-orchestrator` loads in a fresh session. `installed_plugins.json`
   does not list it today.
2. Create a real brief with `milestone new`. Confirm
   `set-status --status MILESTONE_ACTIVE` is **refused** while a section is
   empty, and succeeds once it is filled. Run `milestone sync` and see one
   resolved slice and one `not yet specced` slice rendered.
3. Close a real slice with `set-status --status VERIFIED_CLOSED` and confirm its
   checkbox is ticked in the brief without a second command.
