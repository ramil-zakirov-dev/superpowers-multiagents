"""Abandonment is a derived fact: in-progress status + no live supervisor.

Every case injects liveness, so these tests never touch a real process table.
"""

import json
import time

from scripts import abandonment
from scripts.config import load_agent_config
from scripts.locks import (
    LOCK_START_GRACE_SECONDS,
    acquire_slice_lock,
    claim_slice_lock,
)
from scripts.paths import lock_path

ALWAYS_ALIVE = lambda pid: True
ALWAYS_DEAD = lambda pid: False

IN_PROGRESS = {"PLANNING", "EXECUTING"}


def _running_lock(project_root, pid, slice_id="slice-01"):
    lock_file = acquire_slice_lock(slice_id, project_root)
    claim_slice_lock(lock_file, pid)
    return lock_file


def test_alive_supervisor_plus_in_progress_status_is_not_abandoned(tmp_path):
    _running_lock(tmp_path, pid=1234)
    assert not abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_ALIVE
    )


def test_dead_supervisor_plus_in_progress_status_is_abandoned(tmp_path):
    _running_lock(tmp_path, pid=1234)
    assert abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_dead_supervisor_plus_terminal_status_is_not_abandoned(tmp_path):
    """The dispatch ended; the runner recorded it before dying."""
    _running_lock(tmp_path, pid=1234)
    assert not abandonment.is_abandoned(
        "EXECUTION_COMPLETE", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_no_lock_at_all_is_not_abandonment_but_hand_ownership(tmp_path):
    """#24: an in-progress status no longer implies a supervisor.

    Dispatch acquires the lock *before* writing the in-progress status, so a
    document sitting at one with no lock was never dispatched into it — a human
    claimed it, which is a legal way for work to be in progress. Convicting
    that as abandonment told every hand-driven slice to run `reconcile`.
    """
    assert not abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_ALIVE
    )
    assert abandonment.is_hand_owned("PLANNING", "slice-01", tmp_path, IN_PROGRESS)


def test_a_dead_dispatch_is_not_hand_owned(tmp_path):
    """The two readings are exclusive: a lock is the evidence separating them."""
    _running_lock(tmp_path, pid=1234)

    assert not abandonment.is_hand_owned("EXECUTING", "slice-01", tmp_path, IN_PROGRESS)
    assert abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_settled_document_is_neither(tmp_path):
    assert not abandonment.is_hand_owned(
        "PLAN_GENERATED", "slice-01", tmp_path, IN_PROGRESS
    )


def test_the_gate_a_role_was_dispatched_from_is_derived_from_its_config(tmp_path):
    """What a failed or abandoned dispatch returns the document to."""
    from scripts.config import DEFAULT_CONFIG

    assert abandonment.gate_for_in_progress(DEFAULT_CONFIG, "EXECUTING") == "PLAN_APPROVED"
    assert abandonment.gate_for_in_progress(DEFAULT_CONFIG, "PLANNING") == "SPEC_APPROVED"
    assert abandonment.gate_for_in_progress(DEFAULT_CONFIG, "PLAN_GENERATED") == ""


def test_an_ambiguous_gate_resolves_to_nothing(tmp_path):
    """Two gates, no answer: a role that may be dispatched from either leaves
    no single place to return to, and guessing would rewrite history."""
    config = {
        "agents": {
            "executor": {
                "in_progress_status": "EXECUTING",
                "allowed_statuses": ["PLAN_APPROVED", "MERGE_CONFLICT"],
            }
        }
    }

    assert abandonment.gate_for_in_progress(config, "EXECUTING") == ""


def test_missing_lock_plus_terminal_status_is_not_abandoned(tmp_path):
    assert not abandonment.is_abandoned(
        "PLAN_GENERATED", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_starting_lock_inside_the_grace_window_is_not_abandoned(tmp_path):
    """Dispatch sets the in-progress status BEFORE spawning the supervisor;
    the gap between those two events must not read as abandonment."""
    acquire_slice_lock("slice-01", tmp_path)
    assert not abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_starting_lock_past_the_grace_window_is_abandoned(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    data["started_at"] = time.time() - (LOCK_START_GRACE_SECONDS + 1)
    lock_file.write_text(json.dumps(data), encoding="utf-8")
    assert abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_corrupt_lock_is_abandoned(tmp_path):
    lock_file = lock_path(tmp_path, "slice-01")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("not json at all", encoding="utf-8")
    assert abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_in_progress_statuses_come_from_config_never_literals(tmp_path):
    """The regression that keeps the check honest for anyone who is not us:
    a project that renames EXECUTING must be detected by its own word."""
    (tmp_path / ".superpowers").mkdir()
    (tmp_path / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  executor:\n"
        "    in_progress_status: WORKING\n",
        encoding="utf-8",
    )
    config = load_agent_config(tmp_path)
    in_progress = abandonment.in_progress_statuses(config)

    assert "WORKING" in in_progress
    _running_lock(tmp_path, pid=1234)
    assert abandonment.is_abandoned(
        "WORKING", "slice-01", tmp_path, in_progress, is_alive=ALWAYS_DEAD
    )
    assert not abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, in_progress, is_alive=ALWAYS_DEAD
    )


def test_lock_evidence_names_the_dead_pid(tmp_path):
    _running_lock(tmp_path, pid=41676)
    evidence = abandonment.lock_evidence("slice-01", tmp_path, is_alive=ALWAYS_DEAD)
    assert "41676" in evidence
    assert "not alive" in evidence


def test_lock_evidence_names_the_absent_lock(tmp_path):
    evidence = abandonment.lock_evidence("slice-01", tmp_path, is_alive=ALWAYS_DEAD)
    assert "no lock file" in evidence
