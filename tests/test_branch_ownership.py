"""Dispatch guards for the branch and worktree the plugin owns.

The mechanism is `git worktree add -b feat/<slice_id> .worktrees/<slice_id> HEAD`,
which has two consequences a user cannot see and will not guess: a hand-made
`feature/<slice_id>` is a different branch one character away, and an isolated
role only ever sees what HEAD committed.
"""

import subprocess

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import is_tracked_at_head
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import lock_path


class _Args:
    def __init__(self, file, role="planner"):
        self.file = str(file)
        self.role = role
        self.model = None
        self.dir = None


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _make_isolated(tmp_project):
    """Turn the fixture's planner into a role that runs in its own worktree."""
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: 'import stub_agent'\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: true\n",
        encoding="utf-8",
    )
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "isolated planner")


def test_is_tracked_at_head_sees_the_committed_file(tmp_project, demo_spec):
    assert is_tracked_at_head(demo_spec, tmp_project) is True


def test_is_tracked_at_head_rejects_an_uncommitted_file(tmp_project):
    stray = tmp_project / "docs" / "superpowers" / "specs" / "not-committed.md"
    stray.write_text("---\nslice_id: 'nope'\n---\n", encoding="utf-8")
    assert is_tracked_at_head(stray, tmp_project) is False


def test_dispatching_an_isolated_role_at_an_uncommitted_document_is_refused(tmp_project):
    """The worktree forks from HEAD, so an uncommitted document is not in it.

    The run would start, find nothing at the path it was handed, and fail in
    whatever way the harness fails — a long way from the cause.
    """
    _make_isolated(tmp_project)
    spec = tmp_project / "docs" / "superpowers" / "specs" / "2026-08-04-slice-02-design.md"
    spec.write_text(
        '---\nslice_id: "slice-02-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo 2\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_Args(spec))

    assert parse_frontmatter(spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert not lock_path(tmp_project, "slice-02-demo").exists()
    assert not (tmp_project / ".worktrees").exists()


def test_the_refusal_names_the_branch_the_document_has_to_be_on(tmp_project, capsys):
    _make_isolated(tmp_project)
    spec = tmp_project / "docs" / "superpowers" / "specs" / "2026-08-04-slice-02-design.md"
    spec.write_text(
        '---\nslice_id: "slice-02-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo 2\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_Args(spec))

    out = capsys.readouterr().out
    assert "commit" in out.lower()
    assert str(spec.name) in out


def test_a_non_isolated_role_is_dispatched_from_disk_uncommitted_and_all(tmp_project):
    """The planner reads the project root, so HEAD is not its horizon."""
    spec = tmp_project / "docs" / "superpowers" / "specs" / "2026-08-04-slice-02-design.md"
    spec.write_text(
        '---\nslice_id: "slice-02-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo 2\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(_Args(spec))          # must not raise

    assert parse_frontmatter(spec.read_text(encoding="utf-8"))["status"] == "PLANNING"


def test_a_near_miss_branch_is_reported(tmp_project, demo_spec, capsys):
    """`feature/<slice_id>` is the branch a human makes by hand; the plugin
    makes `feat/<slice_id>`. Both existing is how a slice ends up half-merged."""
    _git(tmp_project, "branch", "feature/slice-01-demo")

    cmd_dispatch_agent(_Args(demo_spec))

    out = capsys.readouterr().out
    assert "feature/slice-01-demo" in out
    assert "feat/slice-01-demo" in out


def test_no_near_miss_no_noise(tmp_project, demo_spec, capsys):
    cmd_dispatch_agent(_Args(demo_spec))
    assert "feature/" not in capsys.readouterr().out
