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
        "dir": str(base), "slice": "slice-01", "timeout": None, "poll": 15.0,
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


def test_cmd_wait_refuses_an_unknown_slice_id(tmp_path, capsys):
    """Exit 3, not 1. A timeout means "not finished yet, ask again"; an unknown
    slice means "this will never finish". A caller branching on the code must
    be able to tell those apart, or a typo in --slice reads as a slow run and
    it waits for something that does not exist."""
    (tmp_path / ".superpowers").mkdir()

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

    (tmp_path / ".superpowers").mkdir()
    with pytest.raises(SystemExit) as cannot_start:
        cmd_wait(_args(tmp_path))

    assert timed_out.value.code != cannot_start.value.code
