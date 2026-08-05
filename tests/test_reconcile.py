"""reconcile moves an abandoned dispatch's document to FAILED — the truthful
statement about the dispatch — and releases the stale lock. It refuses to
race a live supervisor, and refuses to assert anything without --yes."""

import argparse
import os

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import cmd_reconcile
from scripts.paths import lock_path


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-04-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: EXECUTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return tmp_path, doc


def _args(doc, **overrides):
    return argparse.Namespace(
        **{"file": str(doc), "dir": "", "yes": True, **overrides}
    )


def _dead_lock(project_root, slice_id="slice-01"):
    lock_file = acquire_slice_lock(slice_id, project_root)
    claim_slice_lock(lock_file, 999999999)   # gone on any real process table
    return lock_file


def _status(doc):
    return parse_frontmatter(doc.read_text(encoding="utf-8"))["status"]


def test_reconcile_moves_an_abandoned_dispatch_to_failed(project, capsys):
    root, doc = project
    _dead_lock(root)

    cmd_reconcile(_args(doc))

    assert _status(doc) == "FAILED"
    assert not lock_path(root, "slice-01").exists()
    out = capsys.readouterr().out
    assert "999999999" in out          # the verdict names its grounds
    assert "not alive" in out


def test_reconcile_refuses_a_live_supervisor(project):
    """Reconciling a running dispatch would race the runner's own epilogue."""
    root, doc = project
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid())

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "EXECUTING"
    assert lock_file.exists()


def test_reconcile_refuses_a_terminal_status(project):
    root, doc = project
    doc.write_text(
        doc.read_text(encoding="utf-8").replace("EXECUTING", "EXECUTION_COMPLETE"),
        encoding="utf-8",
    )
    _dead_lock(root)

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "EXECUTION_COMPLETE"
    assert lock_path(root, "slice-01").exists()


def test_reconcile_without_yes_prints_the_evidence_and_mutates_nothing(project, capsys):
    root, doc = project
    _dead_lock(root)

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc, yes=False))

    assert excinfo.value.code == 2
    assert _status(doc) == "EXECUTING"
    assert lock_path(root, "slice-01").exists()
    out = capsys.readouterr().out
    assert "999999999" in out          # evidence is shown even when refusing
    assert "--yes" in out


def test_a_second_reconcile_refuses_cleanly(project):
    """Idempotent in the only sense that matters: no corruption, a clear no."""
    root, doc = project
    _dead_lock(root)
    cmd_reconcile(_args(doc))
    assert _status(doc) == "FAILED"

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "FAILED"


def test_reconcile_a_milestone_is_refused(project, tmp_path):
    root, _doc = project
    milestone = tmp_path / "docs" / "superpowers" / "milestones" / "m.md"
    milestone.parent.mkdir(parents=True)
    milestone.write_text(
        '---\nkind: milestone\nstatus: MILESTONE_ACTIVE\n---\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit):
        cmd_reconcile(_args(milestone))
