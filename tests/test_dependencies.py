from pathlib import Path

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
