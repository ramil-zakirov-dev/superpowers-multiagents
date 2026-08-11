"""wait is a join over a dispatch: it returns when the status leaves the
role's in-progress set (exit 0), when the supervisor dies with the status
unchanged (exit 2), or when the caller's patience runs out (exit 1).

Liveness and the clock are injected into the loop itself, so these are fast
and not flaky. The cmd_wait tests use only first-iteration paths, which need
no injection at all."""

import argparse

import pytest

from scripts import abandonment
from scripts.config import DEFAULT_CONFIG
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import cmd_wait


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def document(tmp_path_factory):
    base = tmp_path_factory.mktemp("document")
    (base / ".superpowers").mkdir()
    docs = base / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-04-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLANNING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return base, doc


def _running_lock(project_root, pid=1234):
    lock_file = acquire_slice_lock("slice-01", project_root)
    claim_slice_lock(lock_file, pid)
    return lock_file


def _set_status(doc, status):
    text = doc.read_text(encoding="utf-8")
    doc.write_text(text.replace("PLANNING", status), encoding="utf-8")


def test_wait_returns_terminal_when_the_status_moves(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()

    def sleep_then_advance(seconds):
        _set_status(doc, "PLAN_GENERATED")     # the runner lands its epilogue
        clock.sleep(seconds)

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        sleep=sleep_then_advance, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_TERMINAL
    assert result.status == "PLAN_GENERATED"
    assert result.elapsed == 15.0


def test_wait_returns_abandoned_when_the_pid_dies_with_status_unchanged(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()
    alive_checks = iter([True, False])         # alive at dispatch, gone a poll later

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: next(alive_checks), poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_ABANDONED
    assert result.status == "PLANNING"


def test_wait_returns_timed_out_when_neither_happens(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", timeout=30.0,
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_TIMED_OUT
    assert result.status == "PLANNING"
    assert result.elapsed >= 30.0


def _args(base, **overrides):
    return argparse.Namespace(**{
        "dir": str(base), "slice": "slice-01", "file": None, "timeout": None, "poll": 15.0,
        **overrides,
    })


def test_cmd_wait_exits_0_for_an_already_terminal_slice(document, capsys):
    root, doc = document
    _set_status(doc, "PLAN_GENERATED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 0
    assert "PLAN_GENERATED" in capsys.readouterr().out


def test_cmd_wait_exits_2_and_names_reconcile_for_an_abandoned_slice(document, capsys):
    root, doc = document
    _running_lock(root, pid=999999999)         # gone on any real process table

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "abandoned" in out
    assert "reconcile" in out


def test_cmd_wait_exits_1_when_the_timeout_elapses(document, capsys):
    root, doc = document
    acquire_slice_lock("slice-01", root)       # starting, inside the grace window

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers", timeout=0))

    assert excinfo.value.code == 1
    assert "Timed out" in capsys.readouterr().out


def _empty_pipeline(root):
    """A real docs base holding no documents — an empty pipeline, not a
    missing one. `wait` has to get past resolving the directory to reach the
    question these tests are about.
    """
    (root / ".superpowers").mkdir(exist_ok=True)
    for name in ("milestones", "specs", "plans"):
        (root / "docs" / "superpowers" / name).mkdir(parents=True, exist_ok=True)
    return root


def test_cmd_wait_refuses_an_unknown_slice_id(tmp_path, capsys):
    """Exit 3, not 1. A timeout means "not finished yet, ask again"; an unknown
    slice means "this will never finish". A caller branching on the code must
    be able to tell those apart, or a typo in --slice reads as a slow run and
    it waits for something that does not exist."""
    _empty_pipeline(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(tmp_path))

    assert excinfo.value.code == 3
    assert "no document" in capsys.readouterr().out.lower()


def test_cmd_wait_separates_cannot_start_from_timed_out(document, tmp_path, capsys):
    """The two codes must not collapse: the same call that times out on a real
    slice must report a different code than a call that cannot start at all."""
    root, _doc = document
    acquire_slice_lock("slice-01", root)

    with pytest.raises(SystemExit) as timed_out:
        cmd_wait(_args(root / "docs" / "superpowers", timeout=0))
    capsys.readouterr()

    _empty_pipeline(tmp_path)
    with pytest.raises(SystemExit) as cannot_start:
        cmd_wait(_args(tmp_path))

    assert timed_out.value.code != cannot_start.value.code


# --- Which document is a wait actually about? (architect fix, audit gate) ---
#
# A slice has TWO documents carrying the same slice_id: the spec the planner
# was dispatched at, and the plan the executor was dispatched at. Resolving
# `--slice` to "whichever turns up first" made `wait` report the spec's
# settled status while the executor was still running — exit 0, in zero
# seconds, for a dispatch in flight. Observed live: the printed status came
# from the spec and the printed log path from the executor run, so the output
# contradicted itself.


@pytest.fixture
def both_documents(tmp_path_factory):
    """A slice at its realistic mid-life: spec settled, plan in flight."""
    base = tmp_path_factory.mktemp("both")
    (base / ".superpowers").mkdir()
    specs = base / "docs" / "superpowers" / "specs"
    plans = base / "docs" / "superpowers" / "plans"
    specs.mkdir(parents=True)
    plans.mkdir(parents=True)
    spec = specs / "2026-08-05-slice-01-design.md"
    spec.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLAN_GENERATED\n---\n\n# Spec\n',
        encoding="utf-8",
    )
    plan = plans / "2026-08-05-slice-01-plan.md"
    plan.write_text(
        '---\nslice_id: "slice-01"\nstatus: EXECUTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return base, spec, plan


def test_cmd_wait_watches_the_document_that_is_in_flight_not_the_first_found(
    both_documents, capsys
):
    """The spec is settled and listed first; the plan is what is running."""
    root, _spec, _plan = both_documents

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    # No lock at all + an in-progress plan == abandoned. Reading the spec
    # instead would have exited 0 on PLAN_GENERATED.
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "EXECUTING" in out
    assert "PLAN_GENERATED" not in out


def test_cmd_wait_refuses_when_two_documents_are_both_in_flight(
    both_documents, capsys
):
    """Ambiguity is not something to guess at: say so and stop."""
    root, spec, _plan = both_documents
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("PLAN_GENERATED", "PLANNING"),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 3
    assert "ambiguous" in capsys.readouterr().out.lower()


def test_cmd_wait_accepts_an_explicit_file_and_skips_resolution(
    both_documents, capsys
):
    """`--file` is the unambiguous form, as it already is for reconcile."""
    root, spec, _plan = both_documents

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers", slice=None, file=str(spec)))

    assert excinfo.value.code == 0
    assert "PLAN_GENERATED" in capsys.readouterr().out


# --- Waking only on success ----------------------------------------------
# The owner's rule for a dispatched slice: a failed run is repaired by hand,
# so returning on FAILED wakes the caller for something it will not act on.
# `until` narrows what counts as the end of the wait without changing what a
# terminal status means anywhere else.


class FlippingClock(FakeClock):
    """A clock whose sleep also advances the world — the document's status
    changes under the loop the way a human's repair and re-dispatch would."""

    def __init__(self, doc, after, status):
        super().__init__()
        self.doc, self.after, self.status, self.sleeps = doc, after, status, 0

    def sleep(self, seconds):
        super().sleep(seconds)
        self.sleeps += 1
        if self.sleeps == self.after:
            text = self.doc.read_text(encoding="utf-8")
            self.doc.write_text(
                text.replace("status: FAILED", f"status: {self.status}"),
                encoding="utf-8")


def test_success_statuses_are_read_from_the_configured_roles():
    assert abandonment.success_statuses(DEFAULT_CONFIG) == {
        "PLAN_GENERATED", "EXECUTION_COMPLETE"}


def test_until_does_not_end_the_wait_on_a_failed_run(document):
    root, doc = document
    _set_status(doc, "FAILED")
    clock = FlippingClock(doc, after=3, status="EXECUTION_COMPLETE")

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        until={"EXECUTION_COMPLETE"},
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: False,
    )

    assert result.outcome == abandonment.OUTCOME_TERMINAL
    assert result.status == "EXECUTION_COMPLETE"
    assert clock.sleeps == 3      # it really waited through the failure


def test_without_until_a_failed_run_still_ends_the_wait(document):
    """The default is unchanged: every existing caller keeps its semantics."""
    root, doc = document
    _set_status(doc, "FAILED")

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", is_alive=lambda pid: False)

    assert result.outcome == abandonment.OUTCOME_TERMINAL
    assert result.status == "FAILED"


def test_until_still_honours_the_timeout(document):
    """Waiting for a repair that never comes must not be unbounded — the
    caller's patience is the escape, and it has to survive this mode."""
    root, doc = document
    _set_status(doc, "FAILED")
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        until={"EXECUTION_COMPLETE"}, timeout=30, poll=15,
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: False,
    )

    assert result.outcome == abandonment.OUTCOME_TIMED_OUT
    assert result.status == "FAILED"


def test_until_does_not_end_the_wait_on_an_abandoned_dispatch(document):
    """Abandonment is the same class as FAILED: it needs a human, so it is
    not a reason to wake a caller that only acts on success."""
    root, doc = document
    _running_lock(root, pid=999999999)         # gone on any real process table
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        until={"PLAN_GENERATED"}, timeout=30, poll=15,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )

    assert result.outcome == abandonment.OUTCOME_TIMED_OUT


def test_cmd_wait_until_success_keeps_waiting_on_a_failed_slice(document, capsys):
    root, doc = document
    _set_status(doc, "FAILED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers",
                       until_success=True, timeout=0, poll=0))

    assert excinfo.value.code == 1                  # timed out, not "finished"
    assert "FAILED" in capsys.readouterr().out


def test_cmd_wait_without_the_flag_reports_a_failed_slice_as_a_failure(
    document, capsys
):
    """This asserted exit 0 until slice 08, and the name said "as before".

    Before was wrong. `0` and "the wait ended" were the same code, so the
    caller that backgrounds this — the only caller the flag exists for — got
    the same signal whether the plan was ready or the run had died and put the
    slice back at its gate.
    """
    root, doc = document
    _set_status(doc, "FAILED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 4
    assert "FAILED" in capsys.readouterr().out


def test_cmd_wait_reports_a_gate_return_as_a_failure_too(document, capsys):
    """The 2026-08-09 run, reduced.

    A dispatch that ends at its gate is the shape 2.17.0 introduced, and it is
    the one most easily mistaken for success: the status is a perfectly
    ordinary one that a human might also have set on purpose. Nothing will
    move it on its own, which is what the exit code has to convey.
    """
    root, doc = document
    _set_status(doc, "PLAN_APPROVED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 4
    out = capsys.readouterr().out
    assert "PLAN_APPROVED" in out
    assert "success" in out, "the line must say what did not happen"


def test_cmd_wait_still_exits_0_only_for_a_role_success_status(document, capsys):
    """The other half: narrowing 0 must not have narrowed it to nothing."""
    root, doc = document
    _set_status(doc, "EXECUTION_COMPLETE")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 0
    assert "EXECUTION_COMPLETE" in capsys.readouterr().out
