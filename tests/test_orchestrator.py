import pytest
import tempfile
from pathlib import Path
from scripts.orchestrator import (
    parse_frontmatter,
    update_frontmatter_status,
    load_project_hooks,
    run_infrastructure_hook,
    check_unmet_dependencies
)


def test_parse_frontmatter():
    sample_md = """---
milestone_id: 1
slice_id: "slice-01"
status: SPEC_APPROVED
depends_on:
  - "slice-00-base"
---

# Feature Title

Some content here.
"""
    data = parse_frontmatter(sample_md)
    assert data["milestone_id"] == "1"
    assert data["slice_id"] == "slice-01"
    assert data["status"] == "SPEC_APPROVED"
    assert data["depends_on"] == ["slice-00-base"]


def test_update_frontmatter_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec-design.md"
        spec_file.write_text("""---
title: "Test Feature"
status: DRAFT_SPEC
---

# Content
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "SPEC_APPROVED") is True
        
        updated_data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert updated_data["status"] == "SPEC_APPROVED"


def test_update_invalid_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec-design.md"
        spec_file.write_text("""---
status: DRAFT_SPEC
---
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "INVALID_STATE") is False


def test_load_project_hooks():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_slice_execution_start:
    command: "echo test"
""", encoding="utf-8")

        hooks = load_project_hooks(project_root)
        assert "on_slice_execution_start" in hooks
        assert hooks["on_slice_execution_start"]["command"] == "echo test"


def test_run_infrastructure_hook():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_slice_execution_start:
    command: "echo LOOPBACK_IP=127.0.0.9"
    capture_env: true
""", encoding="utf-8")

        env = run_infrastructure_hook("on_slice_execution_start", project_root=project_root)
        assert env.get("LOOPBACK_IP") == "127.0.0.9"


def test_check_unmet_dependencies():
    with tempfile.TemporaryDirectory() as tmpdir:
        specs_dir = Path(tmpdir) / "specs"
        specs_dir.mkdir()

        dep_spec = specs_dir / "2026-07-25-slice-01-base-design.md"
        dep_spec.write_text("""---
slice_id: "slice-01-base"
status: EXECUTING
---
""", encoding="utf-8")

        target_spec = specs_dir / "2026-07-25-slice-02-dep-design.md"
        target_spec.write_text("""---
slice_id: "slice-02-dep"
status: SPEC_APPROVED
depends_on:
  - "slice-01-base"
---
""", encoding="utf-8")

        unmet = check_unmet_dependencies(target_spec)
        assert len(unmet) == 1
        assert "slice-01-base" in unmet[0]

        # Update base to VERIFIED_CLOSED
        update_frontmatter_status(dep_spec, "VERIFIED_CLOSED")
        unmet_after = check_unmet_dependencies(target_spec)
        assert len(unmet_after) == 0
