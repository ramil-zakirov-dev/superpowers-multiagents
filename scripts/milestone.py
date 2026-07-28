"""The milestone document kind: its lifecycle, its brief, and its track region.

The orchestrator has exactly two document kinds. A slice is dispatched to an
agent, owns a git branch, and ends in `VERIFIED_CLOSED`. A milestone is never
dispatched, owns no branch, and ends in `MILESTONE_CLOSED` when a human says the
objective was met. Everything that differs between them lives here.
"""

from pathlib import Path

from scripts.errors import ValidationError

MILESTONE_KIND = "milestone"
SLICE_KIND = "slice"

#: The kind of a document that does not declare one. Every document that
#: existed before this module was written is a slice, so this default is what
#: makes the field opt-in rather than a migration.
DEFAULT_KIND = SLICE_KIND

#: Fixed, deliberately not configurable. Both transitions out of DRAFT and
#: ACTIVE are gated on hard-coded checks keyed to these exact names; a project
#: that renamed a status would silently detach its gate. The slice machine
#: already demonstrates the hazard — it is advertised as configurable while
#: `orchestrator.cmd_set_status` compares against the literal
#: "VERIFIED_CLOSED".
MILESTONE_STATUSES = ["MILESTONE_DRAFT", "MILESTONE_ACTIVE", "MILESTONE_CLOSED"]

MILESTONE_TRANSITIONS = {
    "MILESTONE_DRAFT": ["MILESTONE_ACTIVE"],
    "MILESTONE_ACTIVE": ["MILESTONE_DRAFT", "MILESTONE_CLOSED"],
    "MILESTONE_CLOSED": [],
}

#: What "closed" means for each kind. A milestone's terminal status is not
#: `VERIFIED_CLOSED`, so the dependency gate has to ask the document's kind
#: rather than compare against one string.
TERMINAL_STATUS = {
    SLICE_KIND: "VERIFIED_CLOSED",
    MILESTONE_KIND: "MILESTONE_CLOSED",
}

#: The directory name whose contents are expected to declare `kind: milestone`.
MILESTONES_DIRNAME = "milestones"


def document_kind(frontmatter: dict) -> str:
    """The declared kind, or `slice` when none is declared."""
    return frontmatter.get("kind") or DEFAULT_KIND


def is_closed(frontmatter: dict) -> bool:
    """True when the document sits at the terminal status of its own kind."""
    kind = document_kind(frontmatter)
    return frontmatter.get("status") == TERMINAL_STATUS.get(kind)


def check_kind_declaration(path: Path, frontmatter: dict) -> None:
    """Refuse a file in `milestones/` that does not declare `kind: milestone`.

    This does not infer kind from location. It detects a contradiction between
    where a file lives and what it says it is, and stops — which turns an
    otherwise silent misreading (a milestone validated against the slice state
    machine, or merged as if it owned a branch) into one accurate sentence.

    The converse is deliberately unguarded: a correctly declared milestone
    works wherever it is stored.
    """
    if Path(path).parent.name != MILESTONES_DIRNAME:
        return
    if document_kind(frontmatter) == MILESTONE_KIND:
        return
    raise ValidationError(
        f"{path} is in {MILESTONES_DIRNAME}/ but does not declare "
        f"`kind: {MILESTONE_KIND}` in its frontmatter. Add it, or move the file "
        f"if it is a slice document."
    )


def machine_for(kind: str, config: dict) -> tuple[list, dict]:
    """The (valid_statuses, transitions) pair that governs this kind."""
    if kind == MILESTONE_KIND:
        return list(MILESTONE_STATUSES), dict(MILESTONE_TRANSITIONS)
    state_machine = config.get("state_machine") or {}
    return state_machine.get("valid_statuses") or [], state_machine.get("transitions") or {}
