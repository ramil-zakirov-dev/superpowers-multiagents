"""What the status report shows, and what it refuses to bury.

`UNKNOWN` used to mean two unrelated things at once: a document written before
the plugin existed, and a document that claims a state the machine does not
have. The first is not a defect and there can be hundreds of them; the second is
a defect and there is usually one. Printing them identically made the report
grow with repository history while saying less.
"""

import argparse
import json
import os

import pytest


def _args(base, **overrides):
    return argparse.Namespace(**{"dir": str(base), "all": False, **overrides})


def _doc(base, kind, name, text):
    directory = base / kind
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


@pytest.fixture
def base(tmp_path):
    return tmp_path / "docs" / "superpowers"


def _status(base, **overrides):
    from scripts.orchestrator import cmd_status

    cmd_status(_args(base, **overrides))


def test_a_tracked_document_is_listed(base, capsys):
    _doc(base, "specs", "s.md", '---\nslice_id: "a"\nstatus: SPEC_APPROVED\n---\n')
    _status(base)
    out = capsys.readouterr().out
    assert "s.md" in out and "SPEC_APPROVED" in out


def test_a_document_with_no_status_is_counted_not_listed(base, capsys):
    """The 178-row case: history that predates the plugin is not news."""
    for index in range(3):
        _doc(base, "specs", f"old-{index}.md", "# An old design doc\n")
    _doc(base, "specs", "live.md", '---\nslice_id: "a"\nstatus: PLANNING\n---\n')

    _status(base)
    out = capsys.readouterr().out

    assert "live.md" in out
    assert "old-0.md" not in out
    assert "3" in out and "not adopted" in out.lower()


def test_all_lists_what_the_summary_counted(base, capsys):
    _doc(base, "specs", "old.md", "# An old design doc\n")
    _status(base, all=True)
    assert "old.md" in capsys.readouterr().out


def test_a_status_the_machine_does_not_have_is_always_shown(base, capsys):
    """The other half of UNKNOWN, and the half worth interrupting for.

    This document says it is in the machine and names a state that does not
    exist, so nothing will ever move it. Never hidden, flag or no flag.
    """
    _doc(base, "plans", "typo.md", '---\nslice_id: "a"\nstatus: PLAN_APROVED\n---\n')

    _status(base)
    out = capsys.readouterr().out

    assert "typo.md" in out
    assert "PLAN_APROVED" in out
    assert "INVALID" in out


def test_an_unknown_kind_is_invalid_too(base, capsys):
    """`kind: plan` validates as a slice but can never satisfy a gate."""
    _doc(
        base, "plans", "wrong-kind.md",
        '---\nkind: plan\nslice_id: "a"\nstatus: PLAN_GENERATED\n---\n',
    )

    _status(base)
    out = capsys.readouterr().out

    assert "wrong-kind.md" in out
    assert "INVALID" in out
    assert "kind" in out


def test_a_milestone_status_is_valid_for_a_milestone(base, capsys):
    """The two kinds have different vocabularies; neither is invalid."""
    _doc(
        base, "milestones", "m.md",
        '---\nkind: milestone\nstatus: MILESTONE_ACTIVE\ntitle: "M"\n---\n',
    )

    _status(base)
    out = capsys.readouterr().out

    assert "MILESTONE_ACTIVE" in out
    assert "INVALID" not in out


def test_a_slice_status_on_a_milestone_is_invalid(base, capsys):
    _doc(
        base, "milestones", "m.md",
        '---\nkind: milestone\nstatus: PLAN_APPROVED\ntitle: "M"\n---\n',
    )

    _status(base)

    assert "INVALID" in capsys.readouterr().out


def test_an_absent_directory_is_not_reported_as_a_failure(base, capsys):
    """No milestones yet is a fact about the project, not an error."""
    _doc(base, "specs", "s.md", '---\nslice_id: "a"\nstatus: DRAFT_SPEC\n---\n')

    _status(base)
    out = capsys.readouterr().out

    assert "not found" not in out.lower()
    assert "(none)" in out


def test_the_summary_line_is_absent_when_every_document_is_tracked(base, capsys):
    _doc(base, "specs", "s.md", '---\nslice_id: "a"\nstatus: DRAFT_SPEC\n---\n')
    _status(base)
    assert "not adopted" not in capsys.readouterr().out.lower()


def _lock(base, slice_id, payload):
    """A supervisor lock where cmd_status's project-root resolution looks."""
    locks = base / ".superpowers" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{slice_id}.lock").write_text(json.dumps(payload), encoding="utf-8")


def test_an_abandoned_dispatch_is_annotated(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    # pid 999999999 is gone on any real process table — no injection needed.
    _lock(base, "a", {"state": "running", "pid": 999999999})

    _status(base)
    out = capsys.readouterr().out

    assert "EXECUTING" in out          # the stored status is still shown
    assert "abandoned" in out
    assert "999999999" in out
    assert "reconcile" in out


def test_a_live_supervisor_is_not_annotated(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    _lock(base, "a", {"state": "running", "pid": os.getpid()})

    _status(base)

    assert "abandoned" not in capsys.readouterr().out


def test_a_missing_lock_is_reported_as_abandonment(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')

    _status(base)
    out = capsys.readouterr().out

    assert "abandoned" in out
    assert "no lock file" in out


def test_status_mutates_nothing_it_reports(base, capsys):
    """A report that silently repairs is a worse instrument than one that lies."""
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    _lock(base, "a", {"state": "running", "pid": 999999999})
    before_doc = (base / "plans" / "p.md").read_text(encoding="utf-8")
    before_lock = (base / ".superpowers" / "locks" / "a.lock").read_text(encoding="utf-8")

    _status(base)

    assert (base / "plans" / "p.md").read_text(encoding="utf-8") == before_doc
    assert (base / ".superpowers" / "locks" / "a.lock").read_text(encoding="utf-8") == before_lock
