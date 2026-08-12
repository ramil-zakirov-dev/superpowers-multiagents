"""A closed slice has to be able to file itself under a track.

Three closures in one milestone, three hand edits to the brief — and the
machine could not have made any of them, for a reason deeper than the missing
code. `sync_text` updates entries and cannot create one; `briefs_listing`
finds only briefs that already list the slice; and the slice document carries
`milestone_id` and no track, so nothing tells the orchestrator which heading
owns it (issue #35).

The silence made it worse. `_sync_briefs_listing` prints a line per brief it
refreshed and nothing at all for an empty list, so a closure that filed itself
nowhere produced exactly the output of a slice that belongs to no milestone.
The gap surfaces later, at `milestone check`, as a track realised by nothing —
which is the state `close-milestone` refuses on. The pipeline created the
condition quietly and blocked on it much later, with nothing connecting the two.

Two changes, and the order matters: the warning is worth having even where the
key is absent, because a project that never adopts `track:` still deserves to
know its closure filed itself nowhere.
"""

import argparse

import pytest

from scripts import milestone
from scripts.errors import ValidationError
from scripts.orchestrator import _sync_briefs_listing

BRIEF = """---
kind: milestone
milestone_id: "devtools-i18n"
title: "DevTools in two languages"
status: MILESTONE_ACTIVE
---

# DevTools in two languages

## Track decomposition

<!-- tracks:begin -->
### track-1: foundation
depends_on: —

The mechanism and the shell.
- [x] i18n-foundation — VERIFIED_CLOSED

### track-2: operator surface
depends_on: track-1

Playground and Supervisor, the HITL#3 path.

### track-3: authoring surface
depends_on: track-1
<!-- tracks:end -->

## Open questions
"""


# --- the pure insertion ---


def test_an_entry_lands_under_the_track_that_owns_it():
    updated = milestone.insert_entry(BRIEF, "track-2", "i18n-operator-surface")

    lines = updated.split("\n")
    entry = lines.index("- [ ] i18n-operator-surface")
    assert lines.index("### track-2: operator surface") < entry
    assert entry < lines.index("### track-3: authoring surface")


def test_an_entry_goes_after_the_prose_not_before_it():
    """The track's prose explains the track. An entry wedged above it splits
    the explanation from its heading, and the next `sync` has no way to know
    that happened.
    """
    updated = milestone.insert_entry(BRIEF, "track-2", "i18n-operator-surface")

    lines = updated.split("\n")
    assert lines.index("Playground and Supervisor, the HITL#3 path.") < lines.index(
        "- [ ] i18n-operator-surface"
    )


def test_an_entry_joins_the_ones_already_there():
    updated = milestone.insert_entry(BRIEF, "track-1", "i18n-shell-extras")

    lines = updated.split("\n")
    assert lines.index("- [x] i18n-foundation — VERIFIED_CLOSED") < lines.index(
        "- [ ] i18n-shell-extras"
    )


def test_an_entry_at_the_end_of_the_region_stays_inside_it():
    """track-3 is the last track: its block ends at the closing marker rather
    than at another heading, and an entry appended past the marker would be
    invisible to every reader of the region.
    """
    updated = milestone.insert_entry(BRIEF, "track-3", "i18n-authoring-surface")

    lines = updated.split("\n")
    assert lines.index("- [ ] i18n-authoring-surface") < lines.index(
        "<!-- tracks:end -->"
    )
    assert milestone.track_entries(updated) == [
        "i18n-foundation",
        "i18n-authoring-surface",
    ]


def test_inserting_names_the_tracks_that_do_exist_when_the_track_does_not():
    """A typo in `track:` must not file the slice somewhere plausible. The
    refusal carries the real headings because that is what the author needs in
    order to correct it, and guessing here would put a slice under a track
    nobody chose.
    """
    with pytest.raises(ValidationError) as excinfo:
        milestone.insert_entry(BRIEF, "track-9", "i18n-nowhere")

    message = str(excinfo.value)
    assert "track-9" in message
    assert "track-1" in message and "track-3" in message


def test_inserting_an_entry_that_is_already_there_changes_nothing():
    """Idempotent, because `close-slice` may be run twice and because a brief
    edited by hand before the key existed must not gain a duplicate.
    """
    once = milestone.insert_entry(BRIEF, "track-1", "i18n-foundation")

    assert once == BRIEF


def test_a_track_heading_matches_on_its_id_not_its_prose():
    """`### track-2: operator surface` is named `track-2`. Matching the whole
    heading would make the key restate a title that is free to be reworded.
    """
    updated = milestone.insert_entry(BRIEF, "track-2", "x")
    assert "- [ ] x" in updated


# --- what a closure says about where it filed itself ---


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "docs" / "superpowers"
    (base / "plans").mkdir(parents=True)
    (base / "milestones").mkdir(parents=True)
    (base / "milestones" / "2026-08-10-devtools-i18n.md").write_text(
        BRIEF, encoding="utf-8"
    )
    return tmp_path, base


def _plan(base, body):
    path = base / "plans" / "2026-08-12-i18n-authoring-surface.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_closure_that_filed_itself_nowhere_says_so(project, capsys):
    """The silence this issue is about. Today's output for a slice no brief
    lists is byte-identical to the output for a slice with no milestone.
    """
    _root, base = project
    plan = _plan(
        base,
        '---\nslice_id: "i18n-authoring-surface"\n'
        'milestone_id: "devtools-i18n"\nstatus: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("i18n-authoring-surface", plan)

    out = capsys.readouterr().out
    assert "i18n-authoring-surface" in out
    assert "track:" in out, "the warning has to name the key that would fix it"
    assert "milestone check" in out


def test_a_slice_with_a_track_files_itself_under_it(project, capsys):
    _root, base = project
    plan = _plan(
        base,
        '---\nslice_id: "i18n-authoring-surface"\n'
        'milestone_id: "devtools-i18n"\ntrack: "track-3"\n'
        'status: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("i18n-authoring-surface", plan)

    brief = (base / "milestones" / "2026-08-10-devtools-i18n.md").read_text(
        encoding="utf-8"
    )
    lines = brief.split("\n")
    entry = next(
        line for line in lines if "i18n-authoring-surface" in line
    )
    assert entry.startswith("- [x]"), (
        "filing it and syncing it are one step; a slice filed at closure is "
        "closed by definition"
    )
    assert lines.index("### track-3: authoring surface") < lines.index(entry)
    assert "Listed i18n-authoring-surface under track-3" in capsys.readouterr().out


def test_a_slice_with_no_milestone_is_not_nagged(project, capsys):
    """A standalone slice is legitimate — most of this repository's own are.
    Warning there would train the reader to ignore the warning that matters.
    """
    _root, base = project
    plan = _plan(
        base,
        '---\nslice_id: "some-standalone-slice"\nstatus: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("some-standalone-slice", plan)

    assert capsys.readouterr().out == ""


def test_a_brief_that_already_lists_the_slice_is_only_refreshed(project, capsys):
    """Today's behaviour, unchanged, and the reason the insert is guarded by
    `briefs_listing` being empty: a brief edited by hand before `track:`
    existed must not gain a second copy of its own entry.
    """
    _root, base = project
    plan = _plan(
        base,
        '---\nslice_id: "i18n-foundation"\nmilestone_id: "devtools-i18n"\n'
        'track: "track-1"\nstatus: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("i18n-foundation", plan)

    brief = (base / "milestones" / "2026-08-10-devtools-i18n.md").read_text(
        encoding="utf-8"
    )
    assert brief.count("i18n-foundation") == 1
    assert "Refreshed" in capsys.readouterr().out


def test_a_track_that_does_not_exist_is_reported_and_nothing_is_written(
    project, capsys
):
    """A failure to file must not become a failure to close. The slice's
    outcome is already recorded by the time this runs — the same rule the
    completion hook and the sandbox teardown follow.
    """
    _root, base = project
    original = (base / "milestones" / "2026-08-10-devtools-i18n.md").read_text(
        encoding="utf-8"
    )
    plan = _plan(
        base,
        '---\nslice_id: "i18n-authoring-surface"\n'
        'milestone_id: "devtools-i18n"\ntrack: "track-9"\n'
        'status: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("i18n-authoring-surface", plan)

    assert (base / "milestones" / "2026-08-10-devtools-i18n.md").read_text(
        encoding="utf-8"
    ) == original
    assert "track-9" in capsys.readouterr().out


def test_a_milestone_id_naming_no_brief_is_reported(project, capsys):
    _root, base = project
    plan = _plan(
        base,
        '---\nslice_id: "orphan"\nmilestone_id: "no-such-milestone"\n'
        'track: "track-1"\nstatus: VERIFIED_CLOSED\n---\n',
    )

    _sync_briefs_listing("orphan", plan)

    assert "no-such-milestone" in capsys.readouterr().out
