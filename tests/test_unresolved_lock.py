"""A verdict nobody reached has to survive the process that failed to reach it.

2.19.0 gave the runner a third answer. When the watched client dies over a
workspace that is still changing, it writes no status, fires no hook and tears
nothing down — and then, until this slice, released its lock like every other
path and exited. From outside, that is indistinguishable from an abandoned
dispatch: supervisor gone, status never moved. `wait` classified it
`OUTCOME_ABANDONED`, exited `2`, and printed `run reconcile` — which returns
the slice to its gate, the one thing that must not happen while an agent may
still be writing in that tree.

Observed live on 2026-08-12 (issue #34, second occurrence): a *successful* run
— 22 commits, lint clean, 585 tests green when checked by hand afterwards —
left its slice at `EXECUTING` with no signal at all, and the outcome had to be
reconstructed from the log, the branch and a hand-run suite. That
reconstruction is precisely what the exit code was supposed to replace.

So the verdict now outlives the runner, in the one place the runner already
owns: the lock. `state: "unresolved"` is held — no pid, no grace window,
nothing self-heals — until a human resolves it with `certify` or `reconcile`.
"""

import argparse
import json
import os
import sys

import pytest

from scripts import abandonment
from scripts.config import DEFAULT_CONFIG
from scripts.errors import LockError
from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import branch_tip
from scripts.locks import (
    LOCK_STATE_UNRESOLVED,
    acquire_slice_lock,
    claim_slice_lock,
    mark_lock_unresolved,
)
from scripts.orchestrator import (
    _report_wait_result,
    cmd_certify,
    cmd_reconcile,
    cmd_status,
)
from scripts.paths import lock_path
from tests.test_commit_postcondition import _git, _make_slice_branch, _set_status
from tests.test_unknown_outcome import (
    DIES_ALONE,
    DIES_LEAVING_A_WORKER,
    _configure,
    _stop_the_worker,
    _supervise,
)


def _lock_data(project_root, slice_id):
    return json.loads(
        lock_path(project_root, slice_id).read_text(encoding="utf-8")
    )


# --- the runner's side: the lock outlives the process that could not judge ---


def test_an_unobserved_run_leaves_its_lock_behind(tmp_project, demo_spec):
    """The whole point. A released lock says "nothing owns this slice", which
    is a claim about a tree that may still have an agent writing in it.
    """
    _configure(tmp_project, demo_spec, settle=0.5, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    try:
        _supervise(
            tmp_project, demo_spec, worktree,
            [sys.executable, "-B", "-c", DIES_LEAVING_A_WORKER], base,
        )

        assert lock_path(tmp_project, "slice-01-demo").is_file(), (
            "an unresolved run must keep its lock; releasing it invites a "
            "re-dispatch into a live worktree"
        )
        data = _lock_data(tmp_project, "slice-01-demo")
        assert data["state"] == LOCK_STATE_UNRESOLVED
        assert data["verdict"] == "unknown"
        assert data["exit_code"] == 1
        assert data["role"] == "executor"
    finally:
        _stop_the_worker(worktree)


def test_a_settled_run_still_releases_its_lock(tmp_project, demo_spec):
    """The falsifier. Holding the lock on every path would wedge the pipeline
    after every ordinary failure, so the new behaviour has to be reachable
    only through the verdict it belongs to.
    """
    _configure(tmp_project, demo_spec, settle=0.2, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    _supervise(
        tmp_project, demo_spec, worktree,
        [sys.executable, "-B", "-c", DIES_ALONE], base,
    )

    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_the_log_does_not_announce_a_gate_return_that_never_happened(
    tmp_project, demo_spec
):
    """The second half of issue #34, and the more expensive one.

    `_unmet_postcondition` runs before the liveness verdict and printed
    immediately, so the live log read:

        [runner] ERROR: 'executor' left no commits on feat/<slice> ...
        Returning the slice to its gate rather than certifying the run.
        [runner] <slice> was still changing after 1802s ...
        [runner] executor exited 3221226505, but the run's fate was not observed

    The first line is a decision, stated above the line saying no decision was
    reached — and it was false by the time anyone read it, because the agent
    committed 22 times during the watch. A human reading top-down concludes the
    run is a total loss and starts over.
    """
    _configure(tmp_project, demo_spec, settle=0.5, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    try:
        _code, log_file = _supervise(
            tmp_project, demo_spec, worktree,
            [sys.executable, "-B", "-c", DIES_LEAVING_A_WORKER], base,
        )

        log = log_file.read_text(encoding="utf-8")
        assert "not observed" in log
        assert "Returning the slice to its gate" not in log, (
            "a decision must not be announced above the line saying no "
            "decision was reached"
        )
    finally:
        _stop_the_worker(worktree)


def test_a_run_returned_to_its_gate_still_explains_why(tmp_project, demo_spec):
    """The falsifier for the assertion above: suppressing the sentence
    everywhere would cost the operator the one explanation that is true on the
    path that really does return the slice to its gate.
    """
    _configure(tmp_project, demo_spec, settle=0.2, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    _code, log_file = _supervise(
        tmp_project, demo_spec, worktree,
        [sys.executable, "-B", "-c", DIES_ALONE], base,
    )

    log = log_file.read_text(encoding="utf-8")
    assert "left no commits" in log
    assert "Returning the slice to its gate" in log


# --- the lock primitive ---


def test_an_unresolved_lock_is_held(tmp_path):
    """No pid, no grace window. Held means held until a human says otherwise —
    the state exists precisely because nothing can observe its way out.
    """
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")

    with pytest.raises(LockError) as exc:
        acquire_slice_lock("slice-01", tmp_path)

    message = str(exc.value)
    assert "unresolved" in message
    assert "certify" in message and "reconcile" in message, (
        "the refusal has to name the way out; a lock nothing can clear is a "
        "wedged pipeline"
    )


def test_marking_unresolved_keeps_what_the_lock_already_knew(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    claim_slice_lock(lock_file, os.getpid(), role="executor")
    mark_lock_unresolved(lock_file, role="executor", exit_code=42, log="a.log")

    data = json.loads(lock_file.read_text(encoding="utf-8"))
    assert data["slice_id"] == "slice-01"
    assert data["state"] == LOCK_STATE_UNRESOLVED
    assert data["exit_code"] == 42
    assert data["unresolved_at"] > 0


def test_an_unresolved_slice_is_not_abandoned(tmp_path):
    """`is_abandoned` drives the `reconcile` advice, and this is exactly the
    slice that must not be told to reconcile blindly.
    """
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")

    assert not abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, {"EXECUTING"}
    )


def test_status_annotates_an_unresolved_slice(document, capsys):
    """Found by running the CLI, not by reading it — the unit tests above all
    passed while `status` printed a plain `[EXECUTING]` row with no annotation
    at all.

    Both existing branches decline, each correctly: the slice is not abandoned
    (the lock is held) and not hand-owned (a lock exists). Their combined
    silence is the worst available reading of a wedged slice — nothing will
    move it, and the report says nothing.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")

    cmd_status(argparse.Namespace(dir=str(root / "docs" / "superpowers"), all=False))

    out = capsys.readouterr().out
    assert "unresolved" in out
    assert "abandoned" not in out, (
        "an unresolved run is not an abandoned one, and the advice differs"
    )


def test_the_lock_describes_itself_as_unresolved(tmp_path):
    """`status` prints this sentence. Falling through to the generic
    "is in state 'unresolved'" says the word and none of the meaning.
    """
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")

    sentence = abandonment.lock_evidence("slice-01", tmp_path)

    assert "unresolved" in sentence
    assert "certify" in sentence or "reconcile" in sentence


# --- what a waiting caller is told ---


@pytest.fixture
def document(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-12-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: EXECUTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return tmp_path, doc


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_reports_an_unresolved_run_as_its_own_outcome(document):
    """Not abandonment. The advice differs, and on this state the abandoned
    branch's advice — `reconcile` — is actively dangerous.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", timeout=60.0,
        sleep=clock.sleep, monotonic=clock.monotonic, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_UNRESOLVED
    assert result.status == "EXECUTING"


def test_until_success_still_wakes_on_an_unresolved_run(document):
    """`--until-success` deliberately swallows failure and abandonment: the
    caller repairs those by hand and does not want the wakeup. This state is
    different in kind, and the difference is the held lock. A failed slice
    left alone costs nothing; an unresolved one blocks every re-dispatch of
    that slice until someone thinks to look. Silence there means never.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", timeout=60.0,
        sleep=clock.sleep, monotonic=clock.monotonic, poll=15.0,
        until=abandonment.success_statuses(DEFAULT_CONFIG),
    )

    assert result.outcome == abandonment.OUTCOME_UNRESOLVED


def test_wait_exits_5_and_names_both_ways_out(document, capsys):
    """A distinct code, because the caller that backgrounds the wait cannot
    read prose. `2` would send it to `reconcile`, which discards a run that
    may have finished.
    """
    root, doc = document
    result = abandonment.WaitResult(
        abandonment.OUTCOME_UNRESOLVED, "EXECUTING", 30.0,
        evidence="lock is unresolved: 'executor' exited 1 and nothing observed the run's fate",
    )

    code = _report_wait_result("slice-01", doc, root, result, DEFAULT_CONFIG)

    out = capsys.readouterr().out
    assert code == 5
    assert "certify" in out and "reconcile" in out
    assert "unresolved" in out


# --- the two ways out ---


def _args(doc, **overrides):
    return argparse.Namespace(**{"file": str(doc), "dir": "", **overrides})


def test_certify_resolves_a_document_whose_run_went_unresolved(tmp_path, capsys):
    """The runner itself advertises this path: for a producing role it logs
    "read it and run `certify --file …`" and returns `unknown`. Holding the
    lock must not break the instruction the runner prints.
    """
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-12-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLAN_DRAFTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    mark_lock_unresolved(lock_file, role="planner", exit_code=1, log="x.log")

    cmd_certify(_args(doc))

    assert parse_frontmatter(doc.read_text(encoding="utf-8"))["status"] == (
        "PLAN_GENERATED"
    )
    assert not lock_path(tmp_path, "slice-01").exists(), (
        "certifying resolves the run, so the lock it was holding goes with it"
    )


def test_reconcile_repairs_an_unresolved_slice_and_says_what_is_asserted(
    document, capsys
):
    """Reconcile is legal here, but it is a human claim, not an observation:
    nothing the machine can read distinguishes an agent that stopped from one
    that is still writing. So the output has to say what the operator is
    asserting, and `--yes` still stands in front of it.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    mark_lock_unresolved(lock_file, role="executor", exit_code=1, log="x.log")

    with pytest.raises(SystemExit) as exit_info:
        cmd_reconcile(_args(doc, yes=False))

    assert exit_info.value.code == 2
    out = capsys.readouterr().out
    assert "unresolved" in out
    assert ".worktrees" in out, (
        "the operator is asserting something about that tree; name it"
    )

    cmd_reconcile(_args(doc, yes=True))

    assert parse_frontmatter(doc.read_text(encoding="utf-8"))["status"] == (
        "PLAN_APPROVED"
    )
    assert not lock_path(root, "slice-01").exists()


def test_reconcile_still_refuses_under_a_live_supervisor(document, capsys):
    """Unchanged, and the reason it must stay unchanged: a running supervisor
    writes its own epilogue, and racing it is how two parties end up recording
    different outcomes for one run.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid(), role="executor")

    with pytest.raises(SystemExit) as exit_info:
        cmd_reconcile(_args(doc, yes=True))

    assert exit_info.value.code == 1
    assert "live supervisor" in capsys.readouterr().out
