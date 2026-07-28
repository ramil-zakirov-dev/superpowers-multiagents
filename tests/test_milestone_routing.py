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
