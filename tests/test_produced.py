"""The contract a dispatched role's output document has to satisfy."""

import pytest

from scripts.errors import ConfigError
from scripts.produced import find_document, frontmatter_block


def _write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_frontmatter_block_carries_what_the_machine_reads():
    block = frontmatter_block("checkout-flow", "billing-v2", "PLAN_GENERATED")
    assert block == (
        "---\n"
        'slice_id: "checkout-flow"\n'
        'milestone_id: "billing-v2"\n'
        "status: PLAN_GENERATED\n"
        "---"
    )


def test_frontmatter_block_omits_a_milestone_the_source_does_not_have():
    block = frontmatter_block("checkout-flow", "", "PLAN_GENERATED")
    assert "milestone_id" not in block
    assert 'slice_id: "checkout-flow"' in block


def test_frontmatter_block_declares_no_kind():
    """A spec and its plan are the same slice; `kind: plan` is not a kind."""
    assert "kind:" not in frontmatter_block("checkout-flow", "", "PLAN_GENERATED")


def test_find_document_matches_on_slice_id_not_filename(tmp_path):
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    plan = _write(
        tmp_path / "plans", "2026-08-04-anything-at-all.md",
        '---\nslice_id: "checkout-flow"\nstatus: PLAN_GENERATED\n---\n# Plan\n',
    )
    assert find_document(spec, "plans", "checkout-flow") == plan


def test_find_document_ignores_a_document_with_no_frontmatter(tmp_path):
    """The observed failure: a good plan the state machine cannot see."""
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    _write(tmp_path / "plans", "p.md", "# Checkout Flow — Implementation Plan\n")
    assert find_document(spec, "plans", "checkout-flow") is None


def test_find_document_ignores_frontmatter_without_a_status(tmp_path):
    """`approve-plan` moves a status; a document with none cannot pass a gate."""
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    _write(tmp_path / "plans", "p.md", '---\nslice_id: "checkout-flow"\n---\n')
    assert find_document(spec, "plans", "checkout-flow") is None


def test_find_document_ignores_another_slice(tmp_path):
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    _write(
        tmp_path / "plans", "p.md",
        '---\nslice_id: "something-else"\nstatus: PLAN_GENERATED\n---\n',
    )
    assert find_document(spec, "plans", "checkout-flow") is None


def test_find_document_is_none_when_the_directory_does_not_exist(tmp_path):
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    assert find_document(spec, "plans", "checkout-flow") is None


def test_find_document_refuses_a_traversing_directory_name(tmp_path):
    """`produces` names a sibling directory, never a path out of the tree."""
    spec = _write(tmp_path / "specs", "s.md", '---\nslice_id: "checkout-flow"\n---\n')
    with pytest.raises(ConfigError):
        find_document(spec, "../../etc", "checkout-flow")
