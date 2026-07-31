"""Reinforcement appended to a dispatched agent's prompt, from two sources.

A **role** may name skills, in `agents.yaml`. A **document** may cite lenses,
in its own frontmatter. Both are appended to the rendered prompt rather than
substituted into it: `deep_merge` replaces scalars, so a project forced to edit
`prompt_template` in order to mention either would own a fork of the plugin's
default prompt forever.

The document half exists because seeing is not reading. An agent dispatched at
a spec opens it and could notice `lenses:` unaided — but that is a step it may
skip, and naming something in the prompt is a tier above leaving it to be
noticed. This plugin never resolves a citation; it only carries it, so what a
reference means stays the business of whatever serves the catalogue.

Everything here is pure. Whether a skill exists is a question for the harness
adapter, and printing is the orchestrator's job.
"""

SKILL_SENTENCE = "Use these skills where they apply: {names}."
LENS_SENTENCE = "This document cites lenses; read them before you begin: {refs}."


def declared_skills(agent_config: dict) -> list[str]:
    """The role's skill names, de-duplicated, first occurrence winning.

    A repeat is a slip with an unambiguous intent, so it is normalised rather
    than refused — our fail-closed rule is about ambiguity, not untidiness.
    """
    ordered: list[str] = []
    for name in agent_config.get("skills") or []:
        cleaned = name.strip()
        if cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def declared_lenses(frontmatter: dict) -> list[str]:
    """The document's cited parts, de-duplicated, first occurrence winning.

    Lenient by design. This is a field an architect types by hand mid-flow, and
    it is reinforcement rather than a dependency — the same standing as a skill
    the harness cannot see. A malformed entry is dropped and the dispatch goes
    ahead; refusing one would convert an optional improvement into a new way to
    be blocked.
    """
    raw = frontmatter.get("lenses") or []
    if isinstance(raw, str):
        raw = [raw]
    ordered: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def unpinned_lenses(lenses: list[str]) -> list[str]:
    """Citations carrying no version.

    A reference by name alone silently starts meaning different text the day
    its upstream is rewritten, and it is the implementer who discovers that.
    Reported, never refused: the plugin does not own the reference format and
    should not fail a dispatch over a convention it cannot verify.
    """
    return [ref for ref in lenses if "@" not in ref]


def compose_prompt(
    task_prompt: str,
    skills: list[str],
    lenses: list[str] | None = None,
) -> str:
    """Append the skill and lens sentences to a rendered prompt.

    Neither means the prompt is returned untouched: a project that configured
    neither must not pay a single character for them. They are separate
    paragraphs because they answer different questions — what this role is good
    at, and what this document was written against.
    """
    composed = task_prompt
    if skills:
        composed += "\n\n" + SKILL_SENTENCE.format(names=", ".join(skills))
    if lenses:
        composed += "\n\n" + LENS_SENTENCE.format(refs=", ".join(lenses))
    return composed


def invisible_skills(skills: list[str], visible: set[str] | None) -> list[str]:
    """Named skills the harness does not report.

    `visible is None` means the adapter cannot answer, which is not the same as
    answering "none". Returning an empty list keeps the caller quiet instead of
    warning about every correctly-named skill on every custom adapter.
    """
    if visible is None:
        return []
    return [name for name in skills if name not in visible]
