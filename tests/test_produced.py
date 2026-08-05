"""The contract a dispatched role's output document has to satisfy."""

import pytest

from scripts.errors import ConfigError
from scripts.produced import find_document, frontmatter_block


def _write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


#: A spec written to this repository's own convention, which is the thing the
#: generated block was measured against and found short by four keys.
FULL_SOURCE = {
    "slice_id": "checkout-flow",
    "title": "Checkout flow",
    "status": "SPEC_APPROVED",
    "target_version": "2.4.0",
    "depends_on": ["billing-api"],
    "lenses": ["wondelai/release-it#stability-anti-patterns@34ac7339"],
}


def _block(source=None, **overrides):
    options = {
        "slice_id": "checkout-flow",
        "status": "PLAN_GENERATED",
        "source_path": "docs/superpowers/specs/a-design.md",
        "title_template": "{title} implementation plan",
        **overrides,
    }
    return frontmatter_block(dict(source if source is not None else {}), **options)


def test_frontmatter_block_carries_what_the_machine_reads():
    block = _block(FULL_SOURCE)
    assert block == (
        "---\n"
        'slice_id: "checkout-flow"\n'
        'title: "Checkout flow implementation plan"\n'
        "status: PLAN_GENERATED\n"
        'target_version: "2.4.0"\n'
        'spec: "docs/superpowers/specs/a-design.md"\n'
        'depends_on: ["billing-api"]\n'
        "---"
    )


def test_a_dependency_the_source_declares_survives_into_the_produced_document():
    """The sharp one, and sharper than cosmetics: `check_unmet_dependencies`
    reads the *dispatched* document, and the executor is dispatched at the
    plan. A plan that drops the spec's `depends_on` does not merely look
    poorer — it silently disables the gate that was supposed to hold it back.
    """
    assert 'depends_on: ["billing-api"]' in _block(FULL_SOURCE)


def test_a_source_with_no_dependencies_still_says_so():
    """`depends_on: []` is how a human adds one later; omitting the key makes
    the dependency machinery unreachable from a generated document.
    """
    assert "depends_on: []" in _block({"title": "Checkout flow"})


def test_a_lone_dependency_written_as_a_string_is_normalised():
    """`check_unmet_dependencies` tolerates the scalar form, so a source can
    carry it — but the produced document should be the shape everything else
    reads without a special case.
    """
    assert 'depends_on: ["billing-api"]' in _block({"depends_on": "billing-api"})


def test_the_title_is_derived_from_the_source_and_says_what_the_document_is():
    """Measured in the issue: without `title`, `_classify_document` falls back
    to the filename stem and the status row prints the name twice.
    """
    assert 'title: "Checkout flow implementation plan"' in _block(FULL_SOURCE)


def test_a_source_with_no_title_yields_no_invented_one():
    """The same rule `milestone_id` already follows: a key that looks set and
    resolves to nothing is worse than an absent one.
    """
    assert "title:" not in _block({"slice_id": "checkout-flow"})


def test_a_role_with_no_title_template_carries_the_source_title_through():
    """`produces` is configurable, so a role that produces something other
    than a plan may exist. It gets the source's own title rather than a
    hardcoded noun about plans — mediocre, but never wrong.
    """
    assert 'title: "Checkout flow"' in _block(FULL_SOURCE, title_template="")


def test_the_source_path_is_recorded_so_the_plan_points_back_at_its_spec():
    """`close-slice` targets the plan and never reads the spec, so this key is
    the only thing tying the two together for whoever audits one against the
    other.
    """
    assert 'spec: "docs/superpowers/specs/a-design.md"' in _block(FULL_SOURCE)


def test_the_target_version_travels_because_it_is_a_fact_about_the_slice():
    assert 'target_version: "2.4.0"' in _block(FULL_SOURCE)


def test_the_sources_lenses_are_not_copied():
    """Deliberate. `lenses:` records which ways of thinking a document was
    reasoned through; copying the spec's list onto the plan would assert the
    planner used them, and nothing here observed that. The dispatcher already
    puts them in the prompt — asserting the outcome is a different claim.
    """
    assert "lenses" not in _block(FULL_SOURCE)


def test_frontmatter_block_omits_a_milestone_the_source_does_not_have():
    block = _block(FULL_SOURCE)
    assert "milestone_id" not in block
    assert 'slice_id: "checkout-flow"' in block


def test_frontmatter_block_carries_a_milestone_the_source_has():
    block = _block({**FULL_SOURCE, "milestone_id": "billing-v2"})
    assert 'milestone_id: "billing-v2"' in block


def test_frontmatter_block_declares_no_kind():
    """A spec and its plan are the same slice; `kind: plan` is not a kind."""
    assert "kind:" not in _block(FULL_SOURCE)


def test_a_value_carrying_a_quote_does_not_break_the_block():
    """Rendered by hand rather than dumped, so the one thing that can go
    wrong is a title with a double quote in it silently producing YAML the
    pipeline then cannot parse.
    """
    from scripts.frontmatter import parse_frontmatter

    block = _block({"title": 'The "hard" case', "slice_id": "checkout-flow"})
    parsed = parse_frontmatter(block + "\n")
    assert parsed["title"] == 'The "hard" case implementation plan'


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
