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


FILLED_BRIEF = """---
kind: milestone
status: MILESTONE_DRAFT
---

# Milestone 1

## Problem
Operators retype the same answer twenty times a day.

## Users
Support operators; back-office administrators.

## Goals
1. Cut manual retyping.

## Non-goals
**Not in this milestone:** billing.

## Success metrics
| Goal | How we will know |
| --- | --- |
| 1 | Median handle time drops below 4 minutes |

## Constraints & invariants
On-prem only.

## Track decomposition
Split by ownership boundary.

### track-1: Intake
- [ ] slice-01-gateway

## Open questions
Who owns the retention policy? — owner.
"""


def test_a_fully_written_brief_has_no_missing_sections():
    assert milestone.missing_sections(FILLED_BRIEF) == []


@pytest.mark.parametrize("section", milestone.REQUIRED_SECTIONS)
def test_each_required_section_is_reported_when_absent(section):
    """Every section is individually load-bearing, so test them individually."""
    lines = FILLED_BRIEF.splitlines()
    start = lines.index(f"## {section}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    without = "\n".join(lines[:start] + lines[end:])

    assert milestone.missing_sections(without) == [section]


def test_a_section_holding_only_its_template_hint_counts_as_empty():
    """The template's prompts are HTML comments precisely so this is true."""
    text = FILLED_BRIEF.replace(
        "Operators retype the same answer twenty times a day.",
        "<!-- Whose pain, and why now. Include what exists today. -->",
    )

    assert milestone.missing_sections(text) == ["Problem"]


def test_a_multi_line_html_comment_still_counts_as_empty():
    text = FILLED_BRIEF.replace(
        "Operators retype the same answer twenty times a day.",
        "<!-- Whose pain,\nand why now.\n-->",
    )

    assert milestone.missing_sections(text) == ["Problem"]


def test_every_offending_section_is_reported_in_one_run():
    """One attempt should not have to be repeated eight times."""
    text = FILLED_BRIEF.replace("Split by ownership boundary.", "")
    text = text.replace("On-prem only.", "")

    assert milestone.missing_sections(text) == [
        "Constraints & invariants", "Track decomposition"
    ]


def test_the_report_follows_the_canonical_section_order():
    assert milestone.missing_sections("# Empty\n") == list(milestone.REQUIRED_SECTIONS)


def test_heading_matching_is_exact_after_stripping_whitespace():
    """`## Non-goals ` matches; `## Non Goals` and `## non-goals` do not."""
    padded = FILLED_BRIEF.replace("## Non-goals", "##   Non-goals   ")
    assert milestone.missing_sections(padded) == []

    renamed = FILLED_BRIEF.replace("## Non-goals", "## Non Goals")
    assert milestone.missing_sections(renamed) == ["Non-goals"]

    lowered = FILLED_BRIEF.replace("## Non-goals", "## non-goals")
    assert milestone.missing_sections(lowered) == ["Non-goals"]


def test_a_level_three_heading_does_not_satisfy_its_parent_section():
    """Content under `### track-1` belongs to the track, not to the section."""
    text = FILLED_BRIEF.replace("Split by ownership boundary.", "")

    assert "Track decomposition" in milestone.missing_sections(text)
