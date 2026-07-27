import argparse
import subprocess

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import create_git_worktree
from scripts.orchestrator import cmd_set_status


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _set_raw_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("status: SPEC_APPROVED", f"status: {status}"), encoding="utf-8")


def test_plain_status_change_applies(tmp_project, demo_spec):
    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="PLANNING"))
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLANNING"


def test_illegal_verified_closed_source_status_refuses_before_merging(tmp_project, demo_spec):
    """Final-review regression: cmd_set_status used to run the irreversible
    merge + force worktree removal BEFORE checking the transition was legal,
    so an illegal source status (e.g. the spec's default SPEC_APPROVED) got
    silently merged and the worktree deleted while the command still
    reported failure and left the on-disk status unchanged."""
    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    with pytest.raises(SystemExit):
        cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert not (tmp_project / "feature.py").exists(), "merge must not have happened"
    assert worktree.exists(), "worktree must not have been deleted"
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"


def test_verified_closed_merges_then_marks(tmp_project, demo_spec):
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "wip")

    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert (tmp_project / "feature.py").exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "VERIFIED_CLOSED"


def test_conflict_lands_in_merge_conflict_not_verified_closed(tmp_project, demo_spec):
    """The defect: MERGE_CONFLICT was set from the terminal VERIFIED_CLOSED."""
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    (tmp_project / "shared.py").write_text("original\n", encoding="utf-8")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "base")

    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "shared.py").write_text("branch side\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "branch")

    (tmp_project / "shared.py").write_text("main side\n", encoding="utf-8")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "main")

    with pytest.raises(SystemExit):
        cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "MERGE_CONFLICT"


def test_failing_post_merge_hook_does_not_undo_the_merge_or_crash(tmp_project, demo_spec):
    """A failing on_slice_verified_closed hook must not be reported as if the
    merge itself failed -- the merge and status write already succeeded."""
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        'hooks:\n  on_slice_verified_closed:\n    command: "exit 9"\n', encoding="utf-8"
    )
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "wip")

    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert (tmp_project / "feature.py").exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "VERIFIED_CLOSED"
