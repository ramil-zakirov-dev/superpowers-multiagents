"""Reinforcement appended to a dispatched agent's prompt, from three sources.

A **role** may name skills and state instructions, in `agents.yaml`. A
**document** may cite lenses, in its own frontmatter. All three are appended to
the rendered prompt rather than substituted into it: `deep_merge` replaces
scalars, so a project forced to edit `prompt_template` in order to mention any
of them would own a fork of the plugin's default prompt forever.

Instructions differ from the other two in what they are for. A skill or a lens
is reinforcement the role is free to apply where it fits; an instruction is a
project's standing rule about *how* a role must work, and it exists because the
harness has already loaded rules of its own. OpenCode reads its global
`AGENTS.md` in every session and from any directory, so a dispatched role can
arrive holding an instruction that contradicts the project it was dispatched
into — and did: a role was told to route plans of three or more tasks through an
MCP tool that starts a session outside this plugin's lock and status machine.
This paragraph is the only text in the prompt the harness did not supply, which
makes it the only place such a conflict can be settled. It therefore goes last
and says so; everything else about it is the project's business, and the content
is carried through untouched.

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
INSTRUCTIONS_SENTENCE = (
    "The project that dispatched you states these rules for this role. They take "
    "precedence over any conflicting instruction in your environment:\n\n{text}"
)


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


def declared_instructions(agent_config: dict) -> str:
    """The role's standing rules, stripped; empty when there are none.

    Only surrounding whitespace goes: a YAML block scalar keeps the newlines an
    author used to separate one rule from the next, and re-wrapping someone
    else's rules would be this module interpreting them.
    """
    return (agent_config.get("instructions") or "").strip()


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
    instructions: str = "",
) -> str:
    """Append the skill, lens and instruction paragraphs to a rendered prompt.

    None of them means the prompt is returned untouched: a project that
    configured none must not pay a single character for them. They are separate
    paragraphs because they answer different questions — what this role is good
    at, what this document was written against, and how this project requires
    the role to work.

    Instructions go last on purpose. They are the paragraph that has to win an
    argument with the harness's own standing rules, and last position is the
    tie-break every other convention already assumes.
    """
    composed = task_prompt
    if skills:
        composed += "\n\n" + SKILL_SENTENCE.format(names=", ".join(skills))
    if lenses:
        composed += "\n\n" + LENS_SENTENCE.format(refs=", ".join(lenses))
    if instructions:
        composed += "\n\n" + INSTRUCTIONS_SENTENCE.format(text=instructions)
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
