"""The milestone document kind: its lifecycle, its brief, and its track region.

The orchestrator has exactly two document kinds. A slice is dispatched to an
agent, owns a git branch, and ends in `VERIFIED_CLOSED`. A milestone is never
dispatched, owns no branch, and ends in `MILESTONE_CLOSED` when a human says the
objective was met. Everything that differs between them lives here.
"""

import re
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


#: The brief's shape, in the order the template generates it. Presence is
#: enforced; order is not — the template supplies it, and enforcing it would
#: add a failure mode that buys nothing.
REQUIRED_SECTIONS = (
    "Problem",
    "Users",
    "Goals",
    "Non-goals",
    "Success metrics",
    "Constraints & invariants",
    "Track decomposition",
    "Open questions",
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def missing_sections(text: str) -> list[str]:
    """Required sections that are absent or hold nothing but template hints.

    A section is present when a level-2 heading equals its name exactly, after
    stripping surrounding whitespace. It is non-empty when at least one line
    beneath it — up to the next heading of any level — is neither blank, nor
    part of an HTML comment, nor a heading.

    This observes presence, never quality. `Success metrics` in particular can
    be satisfied by a plausible-looking sentence that measures nothing; the
    section is required because forcing the question is worth it, not because
    the check can judge the answer.
    """
    filled: set[str] = set()
    current: str | None = None
    in_comment = False

    for line in text.split("\n"):
        stripped = line.strip()

        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue

        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            current = title if level == 2 else None
            continue

        if current is None or not stripped:
            continue

        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue

        filled.add(current)

    return [section for section in REQUIRED_SECTIONS if section not in filled]
