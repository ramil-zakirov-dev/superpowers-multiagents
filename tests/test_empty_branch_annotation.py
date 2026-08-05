"""A slice that says its work is done, over a branch that carries none.

The supervisor's own check (`runner._unmet_postcondition`) only runs when a
supervisor lived to run it. The documented repair for one that did not — a
human writing the status by hand — reaches the same status with nothing
verified at all, which is precisely how an unchecked slice arrives at
`close-slice`. The report is the second net and costs one `rev-list`.

Derived on read like the abandonment annotation beside it, and keyed on the
success status of whichever roles are isolated rather than on the literal
`EXECUTION_COMPLETE` — a project that renames its statuses must be told the
same truth in its own words.
"""

import argparse
import subprocess

import pytest

from scripts import abandonment
from scripts.config import DEFAULT_CONFIG, load_agent_config
from scripts.orchestrator import cmd_status


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _plan_at(tmp_project, status):
    plans = tmp_project / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / "2026-08-05-slice-01-demo-plan.md"
    plan.write_text(
        f'---\nslice_id: "slice-01-demo"\nstatus: {status}\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return plan


def _make_branch(tmp_project, with_commit):
    worktree = tmp_project / ".worktrees" / "slice-01-demo"
    _git(tmp_project, "worktree", "add", "-b", "feat/slice-01-demo",
         str(worktree), "HEAD")
    if with_commit:
        (worktree / "work.txt").write_text("done", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "feat: the work")
    return worktree


def _status_output(tmp_project, capsys):
    cmd_status(argparse.Namespace(dir=str(tmp_project / "docs" / "superpowers")))
    return capsys.readouterr().out


def test_an_empty_branch_under_a_finished_status_is_flagged(tmp_project, capsys):
    _plan_at(tmp_project, "EXECUTION_COMPLETE")
    _make_branch(tmp_project, with_commit=False)

    out = _status_output(tmp_project, capsys)

    assert "EXECUTION_COMPLETE" in out, "the stored status stays visible"
    assert "feat/slice-01-demo" in out
    assert "no commits" in out


def test_a_branch_with_work_is_not_flagged(tmp_project, capsys):
    _plan_at(tmp_project, "EXECUTION_COMPLETE")
    _make_branch(tmp_project, with_commit=True)

    assert "no commits" not in _status_output(tmp_project, capsys)


def test_a_slice_whose_branch_is_gone_is_not_flagged(tmp_project, capsys):
    """A landed slice tidied its branch away. `close-slice` has its own words
    for a missing branch; repeating them here would make the common, healthy
    case look like a defect.
    """
    _plan_at(tmp_project, "EXECUTION_COMPLETE")

    assert "no commits" not in _status_output(tmp_project, capsys)


def test_other_statuses_are_left_alone(tmp_project, capsys):
    _plan_at(tmp_project, "PLAN_APPROVED")
    _make_branch(tmp_project, with_commit=False)

    assert "no commits" not in _status_output(tmp_project, capsys)


def test_the_report_mutates_nothing(tmp_project, capsys):
    plan = _plan_at(tmp_project, "EXECUTION_COMPLETE")
    _make_branch(tmp_project, with_commit=False)
    before = plan.read_text(encoding="utf-8")

    _status_output(tmp_project, capsys)

    assert plan.read_text(encoding="utf-8") == before


def test_the_flagged_statuses_come_from_config_not_a_literal(tmp_project):
    """A project that renames its statuses must still be checked. The set is
    the success status of every role that owns a worktree — the same shape as
    the in-progress set the abandonment annotation derives.
    """
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  builder:\n"
        "    isolated_worktree: true\n"
        "    success_status: MERGE_CONFLICT\n"
        "  planner:\n"
        "    isolated_worktree: false\n"
        "    success_status: PLAN_GENERATED\n",
        encoding="utf-8",
    )
    # The project's config is deep-merged over the defaults, so the shipped
    # executor is still there — the assertion is about how the set is derived,
    # not about the project replacing it.
    statuses = abandonment.isolated_success_statuses(load_agent_config(tmp_project))

    assert "MERGE_CONFLICT" in statuses, "a renamed status on an isolated role is missed"
    assert "PLAN_GENERATED" not in statuses, "a non-isolated role has no branch to check"
    assert "EXECUTION_COMPLETE" in abandonment.isolated_success_statuses(DEFAULT_CONFIG)
