"""Carrying a document's `lenses:` into the prompt of the agent dispatched at it.

The planner opens the spec and can see the key already. Seeing it is not
reading the parts it cites: that is a step a model may skip, and the whole
reason the `skills:` key exists is that naming something in the prompt is a
tier above leaving it to be noticed.
"""

from scripts.skills import (
    compose_prompt,
    declared_lenses,
    declared_skills,
    unpinned_lenses,
)

PINNED = "wondelai/release-it#stability-anti-patterns@34ac73394a51"
OTHER = "ecc/hexagonal-architecture#architecture-boundaries@97016702dbd5"


def test_a_document_without_the_key_declares_nothing():
    assert declared_lenses({"slice_id": "slice-06", "status": "SPEC_APPROVED"}) == []


def test_an_empty_list_declares_nothing():
    assert declared_lenses({"lenses": []}) == []


def test_entries_keep_their_order():
    assert declared_lenses({"lenses": [PINNED, OTHER]}) == [PINNED, OTHER]


def test_a_repeat_is_normalised_not_refused():
    """Same rule as `skills:` — fail closed on ambiguity, not on untidiness."""
    assert declared_lenses({"lenses": [PINNED, OTHER, PINNED]}) == [PINNED, OTHER]


def test_whitespace_is_stripped():
    assert declared_lenses({"lenses": [f"  {PINNED} "]}) == [PINNED]


def test_a_scalar_is_read_as_one_entry():
    """A hand-written spec citing a single part is the commonest shape."""
    assert declared_lenses({"lenses": PINNED}) == [PINNED]


def test_non_string_entries_are_dropped_rather_than_crashing():
    assert declared_lenses({"lenses": [PINNED, None, 42, ""]}) == [PINNED]


def test_a_prompt_without_lenses_is_untouched():
    assert compose_prompt("Read the spec.", [], []) == "Read the spec."


def test_lenses_append_one_paragraph():
    composed = compose_prompt("Read the spec.", [], [PINNED, OTHER])
    assert composed == (
        "Read the spec.\n\n"
        f"This document cites lenses; read them before you begin: {PINNED}, {OTHER}."
    )


def test_skills_and_lenses_are_separate_paragraphs():
    composed = compose_prompt("Read the spec.", ["clean-code"], [PINNED])
    assert composed.startswith("Read the spec.\n\nUse these skills")
    assert composed.endswith(f"read them before you begin: {PINNED}.")
    assert composed.count("\n\n") == 2


def test_skills_still_compose_without_lenses():
    """The existing two-argument behaviour is unchanged."""
    assert compose_prompt("Read.", ["clean-code"]) == (
        "Read.\n\nUse these skills where they apply: clean-code."
    )


def test_an_unpinned_citation_is_reported():
    """Unpinned, a reference silently starts naming different text one day."""
    assert unpinned_lenses([PINNED, "wondelai/release-it#timeouts"]) == [
        "wondelai/release-it#timeouts"
    ]


def test_pinned_citations_report_nothing():
    assert unpinned_lenses([PINNED, OTHER]) == []


def test_unpinned_citations_do_not_block_a_dispatch():
    """Advisory, like an invisible skill: reinforcement, never a dependency."""
    bare = "wondelai/release-it#timeouts"
    assert declared_lenses({"lenses": [bare]}) == [bare]
    assert bare in compose_prompt("Read.", [], [bare])
