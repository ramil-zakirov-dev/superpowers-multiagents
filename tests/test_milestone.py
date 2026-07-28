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


def _resolver(table):
    """A `resolve` built from a {slice_id: (status, title)} mapping."""
    def resolve(slice_id):
        return table.get(slice_id, (None, None))
    return resolve


TABLE = {
    "slice-01-gateway": ("DRAFT_SPEC", "Gateway intake"),
    "slice-02-native-sandbox": ("VERIFIED_CLOSED", "Native sandbox"),
}


def test_the_checkbox_is_ticked_only_for_verified_closed():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert "- [ ] slice-01-gateway" in out
    assert "- [x] slice-02-native-sandbox" in out


def test_the_suffix_is_regenerated_from_the_slice_frontmatter():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert f"slice-01-gateway{milestone.SEPARATOR}DRAFT_SPEC · Gateway intake" in out


def test_an_unresolvable_slice_is_rendered_not_specced_and_is_not_an_error():
    """A milestone must be able to name slices that do not exist yet."""
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert f"- [ ] slice-04-ledger{milestone.SEPARATOR}{milestone.NOT_SPECCED}" in out


def test_a_resolved_slice_without_a_title_renders_the_status_alone():
    table = dict(TABLE, **{"slice-04-ledger": ("PLAN_APPROVED", None)})

    out = milestone.sync_text(REGION_BRIEF, _resolver(table))

    assert f"- [ ] slice-04-ledger{milestone.SEPARATOR}PLAN_APPROVED\n" in out


def test_the_sync_is_idempotent_byte_for_byte():
    """Idempotency follows from regenerating the suffix, not from patching it."""
    once = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))
    twice = milestone.sync_text(once, _resolver(TABLE))

    assert once == twice


@pytest.mark.parametrize(
    "hostile_title",
    ["Native\nsandbox", "Native\r\nsandbox", "Native\tsandbox", "  padded  "],
)
def test_a_title_with_hostile_whitespace_cannot_break_the_region(hostile_title):
    """The suffix is interpolated into a line-structured document.

    `status` and `title` are read from another document's frontmatter, and YAML
    admits newlines in a scalar. A newline landing inside an entry splits it:
    the tail becomes its own line, the next sync re-renders the now-truncated
    entry and appends the tail again, and the brief grows by one junk line on
    every run — unbounded, because auto-sync fires on every slice closure.
    """
    table = {"slice-01-gateway": ("VERIFIED_CLOSED", hostile_title)}
    resolve = _resolver(table)

    once = milestone.sync_text(REGION_BRIEF, resolve)
    twice = milestone.sync_text(once, resolve)

    assert once == twice, "the sync stopped being idempotent"
    assert once.count("\n") == REGION_BRIEF.count("\n"), (
        "the region gained or lost a line"
    )
    assert milestone.track_entries(once) == milestone.track_entries(REGION_BRIEF), (
        "an entry was corrupted into something that no longer parses as itself"
    )


def test_a_status_with_hostile_whitespace_is_neutralised_too():
    """`status` is `data.get("status", ...)` — raw text from another file."""
    resolve = _resolver({"slice-01-gateway": ("VERIFIED\nCLOSED", "Gateway")})

    once = milestone.sync_text(REGION_BRIEF, resolve)

    assert once.count("\n") == REGION_BRIEF.count("\n")


def test_nothing_outside_the_markers_is_touched():
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    before = out.split(milestone.TRACKS_BEGIN)[0]
    after = out.split(milestone.TRACKS_END)[1]
    assert before == REGION_BRIEF.split(milestone.TRACKS_BEGIN)[0]
    assert after == REGION_BRIEF.split(milestone.TRACKS_END)[1]
    assert "## Open questions" in after


def test_headings_depends_on_lines_and_blank_lines_inside_the_region_survive():
    """Inside the markers the machine owns the checkbox and the suffix. Nothing else."""
    out = milestone.sync_text(REGION_BRIEF, _resolver(TABLE))

    assert "### track-1: Intake" in out
    assert "### track-2: Billing" in out
    assert "depends_on: —" in out
    assert "depends_on: track-1" in out
    assert "\n\n### track-2" in out


def test_a_trailing_newline_is_preserved():
    assert milestone.sync_text(REGION_BRIEF, _resolver(TABLE)).endswith("\n")


def test_a_malformed_entry_aborts_the_whole_sync():
    from scripts.errors import ValidationError

    text = REGION_BRIEF.replace("- [ ] slice-04-ledger", "- [] slice-04-ledger")

    with pytest.raises(ValidationError):
        milestone.sync_text(text, _resolver(TABLE))


def test_progress_counts_closed_over_total():
    assert milestone.progress(REGION_BRIEF, _resolver(TABLE)) == (1, 3)


def test_unclosed_reports_every_open_slice_with_its_status():
    assert milestone.unclosed(REGION_BRIEF, _resolver(TABLE)) == [
        ("slice-01-gateway", "DRAFT_SPEC"),
        ("slice-04-ledger", milestone.NOT_SPECCED),
    ]
