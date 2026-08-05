"""A slice has two documents and one life.

`close-slice` targets the plan, because the plan is where execution's terminal
statuses land. The spec is then left sitting at whatever it said when the
planner finished — forever, because a spec's path ends at `PLAN_GENERATED` and
`VERIFIED_CLOSED` is reachable only from `EXECUTION_COMPLETE`.

Measured in this repository before any of this was written: six of seven specs
misreport. Six are merely misleading, because `dependencies.resolve_document`
prefers `plans/` and the plan answers for the slice. The seventh has no plan at
all — the spec *is* the answer, it reads `SPEC_APPROVED`, and so every slice
that might depend on it is blocked and every milestone listing it can never be
closed.

Closure is therefore **recorded**, not transitioned. A spec does not travel
`EXECUTION_COMPLETE -> VERIFIED_CLOSED`; it never reaches `EXECUTION_COMPLETE`,
which is a claim about a plan having been executed. What is true is that the
slice closed, and that fact belongs on every document carrying its id.
"""

import argparse
import subprocess

import pytest

from scripts.dependencies import check_unmet_dependencies
from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import branch_exists, create_git_worktree
from scripts.orchestrator import cmd_set_status

SLICE = "slice-01-demo"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _status(path):
    return parse_frontmatter(path.read_text(encoding="utf-8")).get("status")


def _set_raw_status(path, status):
    text = path.read_text(encoding="utf-8")
    current = parse_frontmatter(text)["status"]
    path.write_text(text.replace(f"status: {current}", f"status: {status}"), encoding="utf-8")


def _write_plan(project, status="EXECUTION_COMPLETE", slice_id=SLICE):
    plans = project / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"2026-07-26-{slice_id}-plan.md"
    path.write_text(
        f'---\nslice_id: "{slice_id}"\nstatus: {status}\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return path


def _close(path, **overrides):
    return cmd_set_status(argparse.Namespace(
        file=str(path), status="VERIFIED_CLOSED", skip_merge=True, **overrides
    ))


# --- Closing a plan closes the whole slice ----------------------------------


def test_closing_a_plan_closes_its_spec_too(tmp_project, demo_spec):
    """The stale sibling, removed at the moment it would be created."""
    plan = _write_plan(tmp_project)
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    _close(plan)

    assert _status(plan) == "VERIFIED_CLOSED"
    assert _status(demo_spec) == "VERIFIED_CLOSED"


def test_a_spec_left_at_failed_is_closed_when_the_slice_lands(tmp_project, demo_spec):
    """slice-06's real shape: the planner failed once, the slice shipped anyway,
    and the spec has said FAILED ever since. Recording closure is not a
    transition out of FAILED — it is the slice's outcome written down."""
    plan = _write_plan(tmp_project)
    _set_raw_status(demo_spec, "FAILED")

    _close(plan)

    assert _status(demo_spec) == "VERIFIED_CLOSED"


def test_the_closed_spec_is_named_in_the_output(tmp_project, demo_spec, capsys):
    """A command that writes a second file must say which one."""
    plan = _write_plan(tmp_project)
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    _close(plan)

    assert demo_spec.name in capsys.readouterr().out


def test_closing_a_plan_with_no_spec_is_not_an_error(tmp_project, demo_spec):
    """A slice may legitimately have only a plan; the sibling is a bonus."""
    demo_spec.unlink()
    plan = _write_plan(tmp_project)

    _close(plan)

    assert _status(plan) == "VERIFIED_CLOSED"


# --- Repairing a slice that already closed ----------------------------------


def test_a_spec_whose_plan_is_closed_can_be_closed_directly(tmp_project, demo_spec):
    """The repair path for every spec stranded before this existed. Nothing is
    asserted here: the plan on disk says VERIFIED_CLOSED, so the slice's closure
    is observed, not taken on trust."""
    _write_plan(tmp_project, status="VERIFIED_CLOSED")
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    _close(demo_spec)

    assert _status(demo_spec) == "VERIFIED_CLOSED"


def test_a_spec_whose_plan_is_still_open_is_refused_and_names_it(
    tmp_project, demo_spec, capsys
):
    """`close-slice` targets the plan. Saying so beats "invalid transition",
    which names neither the right file nor the reason."""
    plan = _write_plan(tmp_project, status="PLAN_APPROVED")
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    with pytest.raises(SystemExit):
        _close(demo_spec)

    out = capsys.readouterr().out
    assert plan.name in out
    assert "PLAN_APPROVED" in out
    assert _status(demo_spec) == "PLAN_GENERATED"


def test_closing_a_spec_merges_nothing_and_removes_nothing(tmp_project, demo_spec):
    """Recording a closure is not executing one. The merge, the worktree
    removal, the hook and the sandbox teardown all belong to the slice's
    execution, which happened when the plan closed — doing them again on a
    second document would be doing them twice."""
    _write_plan(tmp_project, status="VERIFIED_CLOSED")
    _set_raw_status(demo_spec, "PLAN_GENERATED")
    worktree = create_git_worktree(SLICE, tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    _close(demo_spec)

    assert _status(demo_spec) == "VERIFIED_CLOSED"
    assert worktree.exists(), "the worktree was removed by a spec closure"
    assert branch_exists(f"feat/{SLICE}", tmp_project)
    assert not (tmp_project / "feature.py").exists(), "a merge happened"


# --- The slice that never had a plan ----------------------------------------


def test_a_spec_with_no_plan_is_refused_without_an_assertion(tmp_project, demo_spec, capsys):
    """Nothing on disk says this slice closed. The machine will not invent it."""
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    with pytest.raises(SystemExit):
        cmd_set_status(argparse.Namespace(
            file=str(demo_spec), status="VERIFIED_CLOSED", skip_merge=False
        ))

    assert _status(demo_spec) == "PLAN_GENERATED"
    assert "--skip-merge" in capsys.readouterr().out


def test_a_spec_with_no_plan_closes_under_an_explicit_assertion(tmp_project, demo_spec):
    """`--skip-merge` already means "I assert the work is home; nothing here can
    verify it". A slice implemented outside the pipeline is the same claim."""
    _set_raw_status(demo_spec, "PLAN_GENERATED")

    _close(demo_spec)

    assert _status(demo_spec) == "VERIFIED_CLOSED"


def test_a_slice_in_flight_is_never_closed_by_assertion(tmp_project, demo_spec, capsys):
    """`PLANNING` means a supervisor is, or was, running. Closing over that
    would overwrite a dispatch's own outcome with a human's guess about it —
    and there is a command for a dispatch that never came back."""
    _set_raw_status(demo_spec, "PLANNING")

    with pytest.raises(SystemExit):
        _close(demo_spec)

    assert _status(demo_spec) == "PLANNING"
    assert "reconcile" in capsys.readouterr().out


# --- Why any of this matters ------------------------------------------------


def test_a_closed_spec_stops_blocking_a_dependent_slice(tmp_project, demo_spec):
    """The measured harm, reproduced and then removed.

    With no plan, `resolve_document` answers with the spec — so a spec that can
    never reach a terminal status blocks every slice declaring it, forever.
    """
    _set_raw_status(demo_spec, "PLAN_GENERATED")
    dependent = demo_spec.parent / "2026-07-27-slice-02-design.md"
    dependent.write_text(
        f'---\nslice_id: "slice-02"\nstatus: DRAFT_SPEC\n'
        f'depends_on: ["{SLICE}"]\n---\n\n# Dependent\n',
        encoding="utf-8",
    )

    assert check_unmet_dependencies(dependent), "the premise: it blocks today"

    _close(demo_spec)

    assert check_unmet_dependencies(dependent) == []
