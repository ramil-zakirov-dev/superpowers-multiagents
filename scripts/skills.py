"""Optional per-role skill reinforcement.

A role may name skills it wants a dispatched agent to use. The names are
appended to the rendered prompt rather than substituted into it: `deep_merge`
replaces scalars, so a project forced to edit `prompt_template` in order to
mention a skill would own a fork of the plugin's default prompt forever.

Everything here is pure. Whether a skill exists is a question for the harness
adapter, and printing is the orchestrator's job.
"""

SKILL_SENTENCE = "Use these skills where they apply: {names}."


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


def compose_prompt(task_prompt: str, skills: list[str]) -> str:
    """Append the skill sentence to a rendered prompt.

    No skills means the prompt is returned untouched: a project that never
    configured this must not pay a single character for it.
    """
    if not skills:
        return task_prompt
    return f"{task_prompt}\n\n" + SKILL_SENTENCE.format(names=", ".join(skills))


def invisible_skills(skills: list[str], visible: set[str] | None) -> list[str]:
    """Named skills the harness does not report.

    `visible is None` means the adapter cannot answer, which is not the same as
    answering "none". Returning an empty list keeps the caller quiet instead of
    warning about every correctly-named skill on every custom adapter.
    """
    if visible is None:
        return []
    return [name for name in skills if name not in visible]
