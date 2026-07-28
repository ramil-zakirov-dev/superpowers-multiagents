from pathlib import Path

import pytest

from scripts.dependencies import check_unmet_dependencies


def _spec(directory: Path, name: str, slice_id: str, status: str, depends_on=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---", f'slice_id: "{slice_id}"', f"status: {status}"]
    if depends_on:
        lines.append("depends_on:")
        lines.extend(f'  - "{dep}"' for dep in depends_on)
    lines += ["---", "", "# Body", ""]
    path = directory / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_no_dependencies_is_met(tmp_path):
    spec = _spec(tmp_path / "specs", "a.md", "slice-01", "SPEC_APPROVED")
    assert check_unmet_dependencies(spec) == []


def test_open_dependency_is_reported(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "EXECUTING")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    unmet = check_unmet_dependencies(target)
    assert len(unmet) == 1
    assert "slice-01-base" in unmet[0]
    assert "EXECUTING" in unmet[0]


def test_closed_dependency_is_met(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "VERIFIED_CLOSED")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    assert check_unmet_dependencies(target) == []


def test_slice_id_beats_filename_similarity(tmp_path):
    """Two files whose names both contain the id; frontmatter decides."""
    specs = tmp_path / "specs"
    _spec(specs, "notes-slice-01-base-draft.md", "slice-99-unrelated", "DRAFT_SPEC")
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "VERIFIED_CLOSED")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    assert check_unmet_dependencies(target) == []


def test_ambiguous_dependency_is_reported_as_ambiguous(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "a-slice-01-base.md", "slice-01-base", "VERIFIED_CLOSED")
    _spec(specs, "b-slice-01-base.md", "slice-01-base", "DRAFT_SPEC")
    target = _spec(specs, "target.md", "slice-02", "SPEC_APPROVED", depends_on=["slice-01-base"])
    unmet = check_unmet_dependencies(target)
    assert len(unmet) == 1
    assert "ambiguous" in unmet[0].lower()


def test_missing_dependency_says_not_found(tmp_path):
    specs = tmp_path / "specs"
    target = _spec(specs, "target.md", "slice-02", "SPEC_APPROVED", depends_on=["ghost"])
    assert "not found" in check_unmet_dependencies(target)[0].lower()


def test_dependency_is_found_in_a_sibling_specs_dir(tmp_path):
    """A plan lives in plans/, but depends_on refers to specs/."""
    superpowers = tmp_path / "docs" / "superpowers"
    _spec(superpowers / "specs", "slice-01-design.md", "slice-01", "VERIFIED_CLOSED")
    plan = _spec(superpowers / "plans", "slice-02-plan.md", "slice-02", "PLAN_APPROVED",
                 depends_on=["slice-01"])
    assert check_unmet_dependencies(plan) == []


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
