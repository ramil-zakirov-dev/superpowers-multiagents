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


import datetime


def test_the_template_contains_every_required_section():
    text = milestone.render_template("milestone-1", "Intake automation")

    for section in milestone.REQUIRED_SECTIONS:
        assert f"## {section}" in text


def test_a_fresh_template_reports_every_section_as_empty():
    """The strongest statement that the hints are comments, not content.

    If a hint were ever written as prose, this test goes red — which is the
    only way to notice that the approval gate had quietly become a no-op.
    """
    text = milestone.render_template("milestone-1", "Intake automation")

    assert milestone.missing_sections(text) == list(milestone.REQUIRED_SECTIONS)


def test_the_template_declares_the_kind_and_the_draft_status():
    from scripts.frontmatter import parse_frontmatter

    data = parse_frontmatter(milestone.render_template("milestone-1", "Intake"))

    assert data["kind"] == "milestone"
    assert data["milestone_id"] == "milestone-1"
    assert data["title"] == "Intake"
    assert data["status"] == "MILESTONE_DRAFT"


def test_the_template_carries_both_track_markers():
    text = milestone.render_template("milestone-1", "Intake")

    assert milestone.TRACKS_BEGIN in text
    assert milestone.TRACKS_END in text
    assert text.index(milestone.TRACKS_BEGIN) < text.index(milestone.TRACKS_END)


def test_the_template_states_the_altitude():
    """"High-level yet thorough" is a tension an author resolves downward."""
    text = milestone.render_template("milestone-1", "Intake")

    assert "slice spec" in text


def test_create_writes_a_dated_file_and_returns_its_path(tmp_path):
    path = milestone.create(
        tmp_path, "milestone-1", "Intake", today=datetime.date(2026, 7, 28)
    )

    assert path == tmp_path / "milestones" / "2026-07-28-milestone-1.md"
    assert path.read_text(encoding="utf-8").startswith("---")


def test_create_refuses_to_overwrite(tmp_path):
    from scripts.errors import ValidationError

    milestone.create(tmp_path, "milestone-1", "Intake", today=datetime.date(2026, 7, 28))

    with pytest.raises(ValidationError) as excinfo:
        milestone.create(
            tmp_path, "milestone-1", "Other", today=datetime.date(2026, 7, 28)
        )

    assert "already exists" in str(excinfo.value)


def test_create_rejects_an_unsafe_id(tmp_path):
    from scripts.errors import ValidationError

    with pytest.raises(ValidationError):
        milestone.create(
            tmp_path, "../escape", "Intake", today=datetime.date(2026, 7, 28)
        )


REGION_BRIEF = f"""# M

## Track decomposition

Split by ownership boundary.

{milestone.TRACKS_BEGIN}
### track-1: Intake
depends_on: —
- [ ] slice-01-gateway
- [x] slice-02-native-sandbox{milestone.SEPARATOR}VERIFIED_CLOSED · Native sandbox

### track-2: Billing
depends_on: track-1
- [ ] slice-04-ledger
{milestone.TRACKS_END}

## Open questions

None.
"""


def test_a_plain_line_is_not_an_entry():
    assert milestone.parse_entry("### track-1: Intake") is None
    assert milestone.parse_entry("depends_on: track-1") is None
    assert milestone.parse_entry("") is None


def test_an_entry_without_a_suffix_parses():
    assert milestone.parse_entry("- [ ] slice-01-gateway") == ("", "slice-01-gateway", " ")


def test_an_entry_with_a_suffix_parses_and_drops_it():
    """The suffix is machine-owned and regenerated, so it is never read back."""
    line = f"- [x] slice-02{milestone.SEPARATOR}VERIFIED_CLOSED · Native sandbox"

    assert milestone.parse_entry(line) == ("", "slice-02", "x")


def test_an_indented_entry_keeps_its_indentation():
    assert milestone.parse_entry("  - [ ] slice-01") == ("  ", "slice-01", " ")


def test_a_line_that_looks_like_an_entry_but_is_not_one_is_refused():
    """Never reinterpret a line the author may have meant differently."""
    from scripts.errors import ValidationError

    for line in ("- [] slice-01", "- [ ]slice-01", "- [y] slice-01", "- [ ] two words"):
        with pytest.raises(ValidationError):
            milestone.parse_entry(line)


def test_track_entries_lists_every_slice_id_in_document_order():
    assert milestone.track_entries(REGION_BRIEF) == [
        "slice-01-gateway", "slice-02-native-sandbox", "slice-04-ledger"
    ]


def test_entries_outside_the_markers_are_not_track_entries():
    text = REGION_BRIEF.replace("None.", "- [x] slice-99-unrelated")

    assert "slice-99-unrelated" not in milestone.track_entries(text)


def test_missing_markers_are_refused_with_both_names(tmp_path):
    from scripts.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        milestone.track_entries("# M\n\n## Track decomposition\n\nNothing.\n")

    message = str(excinfo.value)
    assert milestone.TRACKS_BEGIN in message and milestone.TRACKS_END in message


def test_markers_in_the_wrong_order_are_refused():
    from scripts.errors import ValidationError

    text = f"# M\n{milestone.TRACKS_END}\n- [ ] slice-01\n{milestone.TRACKS_BEGIN}\n"

    with pytest.raises(ValidationError):
        milestone.track_entries(text)
