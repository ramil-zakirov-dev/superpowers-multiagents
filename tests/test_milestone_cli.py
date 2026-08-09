import argparse

import pytest

from scripts.orchestrator import cmd_milestone


def _args(action, **kwargs):
    base = dict(action=action, dir="", id="", title="", file="")
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_new_creates_a_brief_and_prints_the_next_command(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    created = list((tmp_path / "milestones").glob("*.md"))
    assert len(created) == 1
    out = capsys.readouterr().out
    assert "MILESTONE_ACTIVE" in out


def test_new_refuses_to_overwrite_and_exits_non_zero(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Other"))

    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().out


from scripts import milestone


def _brief(tmp_path, entries="- [ ] slice-01-demo\n"):
    """A milestone brief on disk, beside a `specs/` sibling."""
    milestones = tmp_path / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True)
    path = milestones / "2026-07-28-milestone-1.md"
    path.write_text(
        "---\nkind: milestone\nmilestone_id: \"milestone-1\"\n"
        "title: \"Intake\"\nstatus: MILESTONE_DRAFT\n---\n\n"
        "# Intake\n\n## Track decomposition\n\nBy boundary.\n\n"
        f"{milestone.TRACKS_BEGIN}\n### track-1: Intake\n{entries}"
        f"{milestone.TRACKS_END}\n\n## Open questions\n\nNone.\n",
        encoding="utf-8",
    )
    return path


def _spec(tmp_path, slice_id, status, title):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{slice_id}-design.md").write_text(
        f'---\nslice_id: "{slice_id}"\ntitle: "{title}"\nstatus: {status}\n---\n\n# X\n',
        encoding="utf-8",
    )


def test_sync_rewrites_the_checkbox_from_the_spec_on_disk(tmp_path, capsys):
    path = _brief(tmp_path)
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "Demo slice")

    cmd_milestone(_args("sync", file=str(path)))

    text = path.read_text(encoding="utf-8")
    assert "- [x] slice-01-demo" in text
    assert "Demo slice" in text
    assert "1/1" in capsys.readouterr().out


def test_sync_is_idempotent_on_disk(tmp_path):
    path = _brief(tmp_path)
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "Demo slice")

    cmd_milestone(_args("sync", file=str(path)))
    once = path.read_bytes()
    cmd_milestone(_args("sync", file=str(path)))

    assert path.read_bytes() == once


def test_sync_refuses_a_document_that_is_not_a_milestone(tmp_path, capsys):
    _spec(tmp_path, "slice-01-demo", "DRAFT_SPEC", "Demo")
    spec = tmp_path / "docs" / "superpowers" / "specs" / "slice-01-demo-design.md"

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("sync", file=str(spec)))

    assert excinfo.value.code == 1
    assert "kind: milestone" in capsys.readouterr().out


def test_sync_aborts_without_writing_when_a_slice_id_is_ambiguous(tmp_path, capsys):
    path = _brief(tmp_path)
    before = path.read_bytes()
    plans = tmp_path / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (plans / name).write_text(
            '---\nslice_id: "slice-01-demo"\nstatus: DRAFT_SPEC\n---\n\n# X\n',
            encoding="utf-8",
        )

    with pytest.raises(SystemExit):
        cmd_milestone(_args("sync", file=str(path)))

    assert path.read_bytes() == before
    assert "ambiguous" in capsys.readouterr().out


def test_check_reports_every_missing_section_and_exits_non_zero(tmp_path, capsys):
    path = _brief(tmp_path)
    text = path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("check", file=str(path)))

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    # `_brief` already fills Track decomposition ("By boundary.") and Open
    # questions ("None."); missing_sections observes presence, not quality, so
    # those two are correctly absent from the report. Every other required
    # section is genuinely blank and must be listed.
    for section in milestone.missing_sections(text):
        assert section in out


def test_check_passes_on_a_complete_brief(tmp_path, capsys):
    path = _brief(tmp_path)
    body = path.read_text(encoding="utf-8")
    for section in milestone.REQUIRED_SECTIONS:
        if f"## {section}" not in body:
            body += f"\n## {section}\n\nWritten.\n"
    body = body.replace("## Open questions\n\nNone.", "## Open questions\n\nNone yet.")
    path.write_text(body, encoding="utf-8")

    cmd_milestone(_args("check", file=str(path)))

    assert "complete" in capsys.readouterr().out.lower()


def test_status_reports_track_progress_for_milestones(tmp_path, capsys):
    from scripts.orchestrator import cmd_status

    path = _brief(tmp_path, entries="- [ ] slice-01-demo\n- [ ] slice-02-demo\n")
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "One")
    _spec(tmp_path, "slice-02-demo", "DRAFT_SPEC", "Two")

    cmd_status(argparse.Namespace(dir=str(tmp_path / "docs" / "superpowers")))

    assert "(1/2 slices closed)" in capsys.readouterr().out


def test_status_survives_a_milestone_whose_markers_are_missing(tmp_path, capsys):
    """A malformed brief must not take down the whole report."""
    from scripts.orchestrator import cmd_status

    milestones = tmp_path / "docs" / "superpowers" / "milestones"
    milestones.mkdir(parents=True)
    (milestones / "broken.md").write_text(
        '---\nkind: milestone\nstatus: MILESTONE_DRAFT\ntitle: "Broken"\n---\n\n# B\n',
        encoding="utf-8",
    )

    cmd_status(argparse.Namespace(dir=str(tmp_path / "docs" / "superpowers")))

    assert "Broken" in capsys.readouterr().out


EMPTY_SECOND_TRACK = (
    "- [ ] slice-01-demo\n\n### track-2: Billing\ndepends_on: —\nLedger and refunds.\n"
)


def _fill_sections(path):
    """Every required section written, so a report is about tracks alone."""
    body = path.read_text(encoding="utf-8")
    for section in milestone.REQUIRED_SECTIONS:
        if f"## {section}" not in body:
            body += f"\n## {section}\n\nWritten.\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_check_names_the_tracks_that_list_no_slice(tmp_path, capsys):
    """"complete — all required sections are filled" was true and useless."""
    path = _fill_sections(_brief(tmp_path, entries=EMPTY_SECOND_TRACK))

    cmd_milestone(_args("check", file=str(path)))

    out = capsys.readouterr().out
    assert "track-2: Billing" in out
    assert "2 tracks, 1 with no slice listed" in out


def test_check_still_passes_because_an_unbuilt_track_is_normal(tmp_path, capsys):
    """Exit code unchanged: at MILESTONE_ACTIVE every track is empty by design.

    Failing here would block the one transition that is supposed to happen
    before any slice exists. Refusal belongs at MILESTONE_CLOSED.
    """
    path = _fill_sections(_brief(tmp_path, entries=EMPTY_SECOND_TRACK))

    cmd_milestone(_args("check", file=str(path)))  # no SystemExit

    assert "complete" in capsys.readouterr().out


def test_check_says_nothing_about_tracks_when_each_lists_a_slice(tmp_path, capsys):
    path = _fill_sections(_brief(tmp_path))

    cmd_milestone(_args("check", file=str(path)))

    assert "no slice listed" not in capsys.readouterr().out


def test_sync_qualifies_its_progress_line_with_the_tracks_that_list_nothing(
    tmp_path, capsys
):
    """`1/1 slices closed` on a two-track milestone is the lie #25 reports."""
    path = _brief(tmp_path, entries=EMPTY_SECOND_TRACK)
    _spec(tmp_path, "slice-01-demo", "VERIFIED_CLOSED", "Demo slice")

    cmd_milestone(_args("sync", file=str(path)))

    out = capsys.readouterr().out
    assert "1/1 slices closed" in out
    assert "2 tracks, 1 with no slice listed" in out
