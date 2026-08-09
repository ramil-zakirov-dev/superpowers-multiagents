import argparse
import subprocess

import pytest

from scripts import milestone
from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import create_git_worktree
from scripts.orchestrator import cmd_set_status


def _args(file, status):
    return argparse.Namespace(file=str(file), status=status)


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


def test_a_file_in_milestones_without_the_kind_field_is_refused_by_dispatch(
    tmp_project, capsys
):
    """The kind gate applies to dispatch-agent too, not only set-status."""
    from scripts.orchestrator import cmd_dispatch_agent

    milestones = tmp_project / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True, exist_ok=True)
    path = milestones / "2026-07-28-milestone-2.md"
    path.write_text('---\ntitle: "Oops"\nstatus: DRAFT_SPEC\n---\n\n# Oops\n', encoding="utf-8")

    with pytest.raises(SystemExit):
        cmd_dispatch_agent(
            argparse.Namespace(role="planner", file=str(path), model=None)
        )

    assert "kind: milestone" in capsys.readouterr().out


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


def test_an_os_error_during_auto_sync_warns_but_does_not_crash(
    tmp_project, monkeypatch, capsys
):
    """A bare OSError (e.g. the brief is locked by another program on Windows,
    or a permissions failure) must degrade the same way a ValidationError
    does -- not escape as an uncaught traceback that skips the hook and the
    sandbox teardown that follow in the same function."""
    from scripts import milestone as milestone_module

    _write_brief(tmp_project, status="MILESTONE_ACTIVE", entries="- [ ] slice-01-demo\n")
    plan_file = _closeable_slice(tmp_project)

    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(milestone_module, "sync_file", boom)

    cmd_set_status(_args(plan_file, "VERIFIED_CLOSED"))

    assert _status_of(plan_file) == "VERIFIED_CLOSED"
    assert "Warning" in capsys.readouterr().out


def test_dispatching_an_agent_against_a_milestone_is_refused(tmp_project, capsys):
    """No role operates on a brief — and the kind gate must be what says so.

    The brief sits at SPEC_APPROVED, which is exactly the state §1.3 of the
    spec describes as reachable: frontmatter is text, and a file written before
    this slice existed carries whatever it was given. That status is one the
    planner accepts, so `allowed_statuses` lets it through and the kind gate is
    the only thing left to refuse.

    An earlier version of this test used MILESTONE_ACTIVE, which no role
    accepts. The state gate refused first, and the assertion "milestone is in
    the output" was satisfied by the *filename* being echoed — so deleting the
    kind gate entirely left this test green. Asserting on the gate's own marker
    is what makes the verdict specific.
    """
    from scripts.orchestrator import cmd_dispatch_agent

    brief = _write_brief(tmp_project, status="SPEC_APPROVED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_dispatch_agent(
            argparse.Namespace(role="planner", file=str(brief), model=None)
        )

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "[Kind Gate]" in out, f"a different gate refused the dispatch: {out!r}"


def test_a_refused_dispatch_leaves_no_lock_and_no_worktree(tmp_project):
    """Same SPEC_APPROVED fixture, for the same reason: at MILESTONE_ACTIVE the
    state gate refuses first, so this would assert nothing about the kind gate's
    own placement relative to the lock."""
    from scripts.orchestrator import cmd_dispatch_agent

    brief = _write_brief(tmp_project, status="SPEC_APPROVED")

    with pytest.raises(SystemExit):
        cmd_dispatch_agent(
            argparse.Namespace(role="planner", file=str(brief), model=None)
        )

    assert not (tmp_project / ".worktrees").exists()
    locks = tmp_project / ".superpowers" / "locks"
    assert not locks.exists() or not list(locks.glob("*.lock"))


def _close(spec):
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "status: SPEC_APPROVED", "status: VERIFIED_CLOSED"
        ),
        encoding="utf-8",
    )


TWO_TRACKS_ONE_EMPTY = (
    "- [ ] slice-01-demo\n\n### track-2: Billing\ndepends_on: —\nLedger and refunds.\n"
)


def test_closing_is_refused_while_a_track_lists_no_slice(tmp_project, demo_spec, capsys):
    """The hole #25 reports: an unbuilt track could not object to closure.

    Every *listed* slice is closed, so the entry-counting gate saw a complete
    milestone — while track-2 is declared and realised by nothing.
    """
    _close(demo_spec)
    path = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries=TWO_TRACKS_ONE_EMPTY
    )

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    assert _status_of(path) == "MILESTONE_ACTIVE"
    assert "track-2: Billing" in capsys.readouterr().out


def test_an_open_slice_and_an_empty_track_are_both_reported_in_one_run(
    tmp_project, capsys
):
    """One run names everything blocking closure, not the first thing it hits."""
    path = _write_brief(
        tmp_project, status="MILESTONE_ACTIVE", entries=TWO_TRACKS_ONE_EMPTY
    )

    with pytest.raises(SystemExit):
        cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    out = capsys.readouterr().out
    assert "slice-01-demo" in out and "track-2: Billing" in out


def test_a_track_that_lists_its_closed_slice_does_not_block_closure(
    tmp_project, demo_spec
):
    _close(demo_spec)
    path = _write_brief(
        tmp_project,
        status="MILESTONE_ACTIVE",
        entries="- [ ] slice-01-demo\n\n### track-2: Billing\n- [ ] slice-01-demo\n",
    )

    cmd_set_status(_args(path, "MILESTONE_CLOSED"))

    assert _status_of(path) == "MILESTONE_CLOSED"
