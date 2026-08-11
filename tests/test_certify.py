"""`certify` is the way out of a document its supervisor could not promote.

A produced document is born drafting, because the agent writes the file the
moment it starts typing. Only the supervisor that watched the run end may say
it is finished — and when that supervisor dies with the run, the document sits
at a status whose one edge nothing can travel. On 2026-08-10 that stranded a
complete 60 KB plan behind a billing error.

`reconcile` is not the way out. It moves a document *back* to its gate; it says
the dispatch went unrecorded, which is true and useless here, because the plan
is written and re-running the planner would only write it again.

So the claim is handed to the one instrument that can make it. `PLAN_GENERATED`
does not assert anything about the world — it asserts that writing stopped, and
no observer can determine that once the writer is gone. A human who has read
the document is a better witness than a process signal, and the quality gate,
`approve-plan`, still comes after and is untouched.
"""

import argparse
import os

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import cmd_certify


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-11-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLAN_DRAFTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return tmp_path, doc


def _args(doc, **overrides):
    return argparse.Namespace(**{"file": str(doc), "dir": "", **overrides})


def _status(doc):
    return parse_frontmatter(doc.read_text(encoding="utf-8"))["status"]


def _rewrite_status(doc, status):
    doc.write_text(
        doc.read_text(encoding="utf-8").replace(
            "status: PLAN_DRAFTING", f"status: {status}"
        ),
        encoding="utf-8",
    )


def test_certify_promotes_a_document_its_supervisor_could_not(project, capsys):
    _root, doc = project

    cmd_certify(_args(doc))

    assert _status(doc) == "PLAN_GENERATED"
    assert "PLAN_GENERATED" in capsys.readouterr().out


def test_certify_refuses_a_status_no_role_produces(project, capsys):
    """The status is the whole precondition, and the refusal names it.

    Certifying is not a general "move this on" — it is one claim about one
    kind of document. Anything else belongs to `set-status` or to a gate.
    """
    _root, doc = project
    _rewrite_status(doc, "PLAN_APPROVED")

    with pytest.raises(SystemExit) as exit_info:
        cmd_certify(_args(doc))

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "PLAN_APPROVED" in out
    assert "PLAN_DRAFTING" in out, "the refusal must say what it would accept"
    assert _status(doc) == "PLAN_APPROVED"


def test_certify_refuses_while_a_live_supervisor_owns_the_slice(project, capsys):
    """A running supervisor will promote the document itself when the run ends.

    Certifying underneath it means two parties writing the same document's
    status from different evidence, and the supervisor's later verdict could
    be `gate` — leaving a plan marked generated for a run that was recorded as
    never having produced one.
    """
    root, doc = project
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid(), role="planner")

    with pytest.raises(SystemExit) as exit_info:
        cmd_certify(_args(doc))

    assert exit_info.value.code == 1
    assert "supervisor" in capsys.readouterr().out.lower()
    assert _status(doc) == "PLAN_DRAFTING"


def test_certify_refuses_a_path_that_is_not_a_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cmd_certify(_args(tmp_path / "nope.md"))

    assert exit_info.value.code == 1
    assert "nope.md" in capsys.readouterr().out
