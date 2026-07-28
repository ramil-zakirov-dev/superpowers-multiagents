from pathlib import Path

import pytest

from scripts import milestone
from scripts.errors import ValidationError


def test_a_document_without_a_kind_field_is_a_slice():
    """Back-compatibility: every document that exists today is a slice."""
    assert milestone.document_kind({}) == milestone.SLICE_KIND
    assert milestone.document_kind({"slice_id": "slice-01"}) == milestone.SLICE_KIND


def test_a_declared_milestone_is_a_milestone():
    assert milestone.document_kind({"kind": "milestone"}) == milestone.MILESTONE_KIND


def test_closed_means_the_terminal_status_of_the_documents_own_kind():
    assert milestone.is_closed({"status": "VERIFIED_CLOSED"})
    assert not milestone.is_closed({"status": "MILESTONE_CLOSED"})
    assert milestone.is_closed({"kind": "milestone", "status": "MILESTONE_CLOSED"})
    assert not milestone.is_closed({"kind": "milestone", "status": "VERIFIED_CLOSED"})


def test_a_file_in_milestones_without_the_kind_field_is_refused(tmp_path):
    """The authoring mistake the opt-in kind field invites.

    Not an inference of kind from location — a contradiction between two
    signals, which is refused rather than resolved by guessing.
    """
    path = tmp_path / "milestones" / "2026-07-28-milestone-1.md"
    path.parent.mkdir(parents=True)

    with pytest.raises(ValidationError) as excinfo:
        milestone.check_kind_declaration(path, {"title": "Intake"})

    assert "kind: milestone" in str(excinfo.value)


def test_a_declared_milestone_outside_milestones_is_accepted(tmp_path):
    """Location is a convention, never a load-bearing input."""
    path = tmp_path / "elsewhere" / "brief.md"
    path.parent.mkdir(parents=True)
    milestone.check_kind_declaration(path, {"kind": "milestone"})


def test_a_slice_outside_milestones_is_accepted(tmp_path):
    path = tmp_path / "specs" / "2026-07-28-slice-01-design.md"
    path.parent.mkdir(parents=True)
    milestone.check_kind_declaration(path, {"slice_id": "slice-01"})


def test_the_milestone_machine_has_three_states_and_no_failed():
    """No agent is dispatched against a milestone, so no exit code, no FAILED."""
    assert milestone.MILESTONE_STATUSES == [
        "MILESTONE_DRAFT", "MILESTONE_ACTIVE", "MILESTONE_CLOSED"
    ]
    assert "FAILED" not in milestone.MILESTONE_STATUSES


def test_the_milestone_machine_transitions():
    transitions = milestone.MILESTONE_TRANSITIONS
    assert transitions["MILESTONE_DRAFT"] == ["MILESTONE_ACTIVE"]
    assert sorted(transitions["MILESTONE_ACTIVE"]) == [
        "MILESTONE_CLOSED", "MILESTONE_DRAFT"
    ]
    assert transitions["MILESTONE_CLOSED"] == []


def test_machine_for_returns_the_kinds_own_vocabulary():
    """Each machine rejects the other's statuses at no extra cost."""
    config = {"state_machine": {
        "valid_statuses": ["DRAFT_SPEC", "VERIFIED_CLOSED"],
        "transitions": {"DRAFT_SPEC": ["VERIFIED_CLOSED"]},
    }}

    statuses, _ = milestone.machine_for(milestone.MILESTONE_KIND, config)
    assert "EXECUTING" not in statuses and "MILESTONE_CLOSED" in statuses

    statuses, _ = milestone.machine_for(milestone.SLICE_KIND, config)
    assert "MILESTONE_CLOSED" not in statuses and "DRAFT_SPEC" in statuses
