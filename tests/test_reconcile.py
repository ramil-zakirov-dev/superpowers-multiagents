"""reconcile returns an abandoned dispatch's document to the gate it was
dispatched from and releases the stale lock. It refuses to race a live
supervisor, refuses to assert anything without --yes, and refuses a document
no dispatch ever owned — that one is a human's, and set-status is their tool."""

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


def test_reconcile_returns_the_document_to_its_gate(project, capsys):
    root, doc = project
    _dead_lock(root)

    cmd_reconcile(_args(doc))

    assert _status(doc) == "PLAN_APPROVED"
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
    assert _status(doc) == "PLAN_APPROVED"

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "PLAN_APPROVED"


def test_reconcile_a_milestone_is_refused(project, tmp_path):
    root, _doc = project
    milestone = tmp_path / "docs" / "superpowers" / "milestones" / "m.md"
    milestone.parent.mkdir(parents=True)
    milestone.write_text(
        '---\nkind: milestone\nstatus: MILESTONE_ACTIVE\n---\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit):
        cmd_reconcile(_args(milestone))


def test_reconcile_refuses_a_document_no_dispatch_ever_owned(project, capsys):
    """#24: in-progress with no lock is a human at work, not a dead dispatch.

    There is no stale lock to release and nothing went unrecorded. Moving the
    status here would take the slice away from whoever is holding it.
    """
    root, doc = project           # EXECUTING, and no lock was ever acquired

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "EXECUTING"
    out = capsys.readouterr().out
    assert "set-status" in out


def test_a_role_with_no_single_gate_falls_back_to_failed(project, capsys):
    """Two gates leave no single place to return to. FAILED is the honest
    answer there — it says the dispatch went unrecorded and nothing more."""
    root, doc = project
    (root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  executor:\n"
        "    allowed_statuses: ['PLAN_APPROVED', 'MERGE_CONFLICT']\n",
        encoding="utf-8",
    )
    _dead_lock(root)

    cmd_reconcile(_args(doc))

    assert _status(doc) == "FAILED"
