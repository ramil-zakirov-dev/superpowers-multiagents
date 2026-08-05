"""Liveness is the evidence behind every abandonment verdict.

`_is_process_alive` decides whether a lock may be reclaimed and whether a
dispatch is abandoned. Both callers act destructively on a False, so a False
has to be a claim about the process — not a shrug about the lookup.

Observed on 2026-08-05: `dispatch --wait` declared a healthy planner run
abandoned after 30s while its supervisor (pid 22776) was alive, and printed
the contradiction in one breath — "lock names supervisor pid 22776, which is
alive" under the verdict "is abandoned". The verdict and its evidence came
from the same function milliseconds apart and disagreed, which is the
signature of a flaky lookup rather than a logic inversion. The implementation
shelled out to `tasklist`; the machine was at that moment failing to spawn
processes at all (`cygheap read copy failed ... Win32 error 299` from an
unrelated `grep` in the same minute), and every failure mode of that
subprocess returned False.
"""

import os
import subprocess

import pytest

from scripts import abandonment
from scripts.config import DEFAULT_CONFIG
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import _report_wait_result
from scripts.utils import _is_process_alive


def test_the_current_process_is_alive():
    assert _is_process_alive(os.getpid()) is True


def test_a_pid_that_cannot_exist_is_not_alive():
    assert _is_process_alive(999999999) is False


def test_liveness_does_not_spawn_a_subprocess(monkeypatch):
    """The regression. A liveness check that forks cannot be trusted in a
    poll loop: the answer starts depending on whether the machine can spawn
    a process right now, and that failure mode reads as "dead" — convicting
    a live supervisor. Nothing about asking whether a pid exists needs a
    child process on any platform we support.
    """
    def refuse(*args, **kwargs):
        raise OSError("simulated fork exhaustion")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    assert _is_process_alive(os.getpid()) is True


def test_an_undeterminable_answer_counts_as_alive(monkeypatch):
    """Fail-closed, and both callers need it that way: lock reclamation must
    not steal a lock it cannot prove is dead, and `wait` must not convict a
    supervisor it merely failed to look up.
    """
    monkeypatch.setattr("scripts.utils._pid_exists", lambda pid: None)

    assert _is_process_alive(4242) is True


# --- the same conflation, one layer up ---


@pytest.fixture
def document(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-05-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLANNING\n---\n\n# Plan\n',
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


def test_wait_does_not_call_an_unreadable_lock_abandoned(document):
    """"I cannot read this" and "nothing owns this slice" are different facts.
    Collapsing them is how a watchdog convicts a living process — the lock is
    rewritten atomically, so a reader can lose a race with the supervisor's
    own claim and see nothing parseable for one poll.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid())
    lock_file.write_text("{ this is not json", encoding="utf-8")
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", timeout=60.0,
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome != abandonment.OUTCOME_ABANDONED


def test_wait_reports_a_persistently_unreadable_lock_as_an_error(document):
    """It cannot wait forever on a lock that will never parse either. After a
    few consecutive unreadable polls it stops — with an outcome that says the
    watcher failed, not that the dispatch did.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    lock_file.write_text("{ this is not json", encoding="utf-8")
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_UNREADABLE_LOCK


def test_the_verdict_carries_the_evidence_it_was_decided_on(document, capsys):
    """The observed line read: "is abandoned after 30s: lock names supervisor
    pid 22776, which is alive." Verdict and grounds disagreed because the
    verdict came from the wait loop and the grounds from a second, later
    lookup. Whatever the reader concludes from that, it cannot be trusted —
    so the grounds travel with the verdict rather than being re-derived when
    it is time to print.
    """
    root, doc = document
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid())        # alive, right now

    result = abandonment.WaitResult(
        abandonment.OUTCOME_ABANDONED, "PLANNING", 30.0,
        evidence="lock names supervisor pid 22776, which is not alive",
    )
    code = _report_wait_result("slice-01", doc, root, result)

    out = capsys.readouterr().out
    assert code == 2
    assert "which is not alive" in out
    assert "which is alive" not in out
