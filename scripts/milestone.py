"""The milestone document kind: its lifecycle, its brief, and its track region.

The orchestrator has exactly two document kinds. A slice is dispatched to an
agent, owns a git branch, and ends in `VERIFIED_CLOSED`. A milestone is never
dispatched, owns no branch, and ends in `MILESTONE_CLOSED` when a human says the
objective was met. Everything that differs between them lives here.
"""

import datetime
import re
from pathlib import Path

from scripts.errors import ValidationError
from scripts.frontmatter import parse_frontmatter
from scripts.utils import _sanitize_id, atomic_write_text

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


TRACKS_BEGIN = "<!-- tracks:begin -->"
TRACKS_END = "<!-- tracks:end -->"

#: Separator between a track entry's slice_id and its machine-owned suffix:
#: space, EM DASH (U+2014), space. `_sanitize_id` admits only alphanumerics,
#: hyphens, underscores and dots, so a slice_id can never contain it and the
#: split is unambiguous.
SEPARATOR = " — "

_TEMPLATE = """---
kind: milestone
milestone_id: "{milestone_id}"
title: "{title}"
status: MILESTONE_DRAFT
---

# {title}

> A milestone is 1-3 months of work and 2-5 tracks. If you are describing a
> screen or an endpoint, you are in the wrong document — that belongs in a
> slice spec.

## Problem

<!-- Whose pain, and why now. Include what exists today and why it is
     insufficient. No solution here. -->

## Users

<!-- Who, in which roles. If this milestone is internal infrastructure, name
     the engineering roles it serves and say so in one line — an honest short
     answer beats invented personas. -->

## Goals

<!-- What becomes true when the milestone is met. One numbered goal per line. -->

## Non-goals

<!-- Two labelled groups.
     **Not in this milestone:** sequencing — we will, but later.
     **Rejected outright:** a stance that outlives this milestone.
     3-7 items; each names something a reasonable person would expect and we
     are deliberately not doing. -->

## Success metrics

<!-- A table with one row per goal above, columns `Goal` and
     `How we will know`. Add an `Overall` row for milestone-wide measures.
     A metric nobody could disagree about having been met is not a metric. -->

## Constraints & invariants

<!-- What must not be violated. One line each. -->

## Track decomposition

<!-- One sentence on why this decomposition and not another. Write it here,
     above the markers: the tracks below are `###` headings and do not count as
     this section's content. -->

{tracks_begin}
### track-1: <name>
depends_on: —
{tracks_end}

## Open questions

<!-- What is unresolved, each with the name of whoever decides it. -->
"""


def render_template(milestone_id: str, title: str) -> str:
    """The brief a `milestone new` writes: PRD form, every hint a comment."""
    return _TEMPLATE.format(
        milestone_id=milestone_id,
        title=title,
        tracks_begin=TRACKS_BEGIN,
        tracks_end=TRACKS_END,
    )


def create(
    directory: Path,
    milestone_id: str,
    title: str,
    today: datetime.date | None = None,
) -> Path:
    """Write a new brief under `<directory>/milestones/`, never overwriting."""
    _sanitize_id(milestone_id, "milestone_id")
    stamp = (today or datetime.date.today()).isoformat()
    target = Path(directory) / MILESTONES_DIRNAME / f"{stamp}-{milestone_id}.md"
    if target.exists():
        raise ValidationError(
            f"{target} already exists. Edit it, or choose another --id."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, render_template(milestone_id, title))
    return target


#: `- [<box>] <slice_id>` with an optional machine-owned suffix. The slice_id
#: is `\S+` because `_sanitize_id` already constrains what a real one may
#: contain; anything with whitespace in it is a malformed entry, not an id.
ITEM_PATTERN = re.compile(
    r"^(?P<indent>\s*)- \[(?P<box>[ xX])\] (?P<slice_id>\S+)"
    rf"(?:{re.escape(SEPARATOR)}.*)?$"
)

#: Anything starting like a list item is held to ITEM_PATTERN.
_LOOKS_LIKE_ENTRY = re.compile(r"^\s*- \[")


def parse_entry(line: str) -> tuple[str, str, str] | None:
    """`(indent, slice_id, checkbox)` for a track entry, else None.

    A line that starts like a list item but does not match the grammar raises
    rather than being skipped: reinterpreting it would silently drop a slice
    from the milestone, and skipping it would hide a typo forever.
    """
    if not _LOOKS_LIKE_ENTRY.match(line):
        return None
    match = ITEM_PATTERN.match(line)
    if not match:
        raise ValidationError(
            f"malformed track entry: {line!r}. Expected "
            f"`- [ ] <slice_id>` or `- [x] <slice_id>`, optionally followed by "
            f"`{SEPARATOR}<generated text>`."
        )
    return match.group("indent"), match.group("slice_id"), match.group("box")


def _region_bounds(text: str) -> tuple[int, int]:
    """Line indices of the two markers. Raises when they are absent or crossed."""
    begin = end = -1
    for index, line in enumerate(text.split("\n")):
        if TRACKS_BEGIN in line and begin == -1:
            begin = index
        elif TRACKS_END in line and end == -1:
            end = index
    if begin == -1 or end == -1 or end < begin:
        raise ValidationError(
            f"track markers not found in the expected order. A milestone brief "
            f"must contain {TRACKS_BEGIN} followed by {TRACKS_END} inside its "
            f"'## Track decomposition' section. Regenerate with `milestone new`, "
            f"or add them by hand."
        )
    return begin, end


def track_entries(text: str) -> list[str]:
    """Every `slice_id` listed between the markers, in document order."""
    begin, end = _region_bounds(text)
    lines = text.split("\n")
    entries = []
    for line in lines[begin + 1:end]:
        parsed = parse_entry(line)
        if parsed is not None:
            entries.append(parsed[1])
    return entries


#: Rendered for an entry naming a slice whose spec has not been written yet.
#: Not an error: a planning document must be able to name what does not exist.
NOT_SPECCED = "not yet specced"


def _render_entry(indent: str, slice_id: str, status, title) -> str:
    if status is None:
        return f"{indent}- [ ] {slice_id}{SEPARATOR}{NOT_SPECCED}"
    box = "x" if status == TERMINAL_STATUS[SLICE_KIND] else " "
    suffix = f"{status} · {title}" if title else status
    return f"{indent}- [{box}] {slice_id}{SEPARATOR}{suffix}"


def sync_text(text: str, resolve) -> str:
    """Rewrite every track entry's checkbox and suffix from live slice status.

    `resolve(slice_id)` returns `(status, title)`, or `(None, None)` when the
    id names nothing on disk. It may raise — an ambiguous id, for instance —
    and the exception propagates so that nothing is written.

    Splitting and rejoining on "\\n" round-trips exactly, including the
    trailing newline, so a file whose entries already read correctly comes back
    byte-identical.
    """
    begin, end = _region_bounds(text)
    lines = text.split("\n")

    for index in range(begin + 1, end):
        parsed = parse_entry(lines[index])
        if parsed is None:
            continue
        indent, slice_id, _box = parsed
        status, title = resolve(slice_id)
        lines[index] = _render_entry(indent, slice_id, status, title)

    return "\n".join(lines)


def progress(text: str, resolve) -> tuple[int, int]:
    """`(closed, total)` over every slice the brief's tracks list."""
    entries = track_entries(text)
    closed = sum(
        1 for slice_id in entries
        if resolve(slice_id)[0] == TERMINAL_STATUS[SLICE_KIND]
    )
    return closed, len(entries)


def unclosed(text: str, resolve) -> list[tuple[str, str]]:
    """`(slice_id, status)` for every listed slice that is not closed."""
    open_entries = []
    for slice_id in track_entries(text):
        status, _title = resolve(slice_id)
        if status == TERMINAL_STATUS[SLICE_KIND]:
            continue
        open_entries.append((slice_id, status or NOT_SPECCED))
    return open_entries


def load(path: Path) -> tuple[dict, str]:
    """Frontmatter and full text of a brief, refusing any other kind.

    `milestone sync` and `milestone check` operate on one document kind. Left
    unchecked they would happily treat a slice spec as a brief and report its
    missing "Problem" section as a milestone defect.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    if document_kind(frontmatter) != MILESTONE_KIND:
        raise ValidationError(
            f"{path} does not declare `kind: {MILESTONE_KIND}`. "
            f"`milestone` commands operate on milestone briefs only."
        )
    return frontmatter, text


def briefs_listing(slice_id: str, start_path: Path) -> list[Path]:
    """Milestone briefs whose track region lists `slice_id`.

    Resolved from the closing document's own location the same way
    `search_dirs_for` resolves siblings, so a project laid out differently
    still finds its milestones. A brief that cannot be read — wrong kind,
    missing markers — is skipped rather than raising: discovery must not be
    able to fail a slice's closure.
    """
    milestones_dir = Path(start_path).parent.parent / MILESTONES_DIRNAME
    if not milestones_dir.is_dir():
        return []

    listing = []
    for candidate in sorted(milestones_dir.glob("*.md")):
        try:
            _frontmatter, text = load(candidate)
            entries = track_entries(text)
        except (OSError, ValidationError):
            continue
        if slice_id in entries:
            listing.append(candidate)
    return listing


def search_dirs_for(brief_path: Path) -> list[Path]:
    """Where a brief's slice ids are resolved: its sibling specs/ and plans/.

    Derived the same way `dependencies._candidate_dirs` derives its siblings,
    so the dependency gate and the track sync cannot disagree about which
    files exist.
    """
    parent = Path(brief_path).parent
    dirs = []
    for sibling in ("plans", "specs"):
        candidate = parent.parent / sibling
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def slice_resolver(search_dirs: list[Path], exclude: Path):
    """A `resolve` over real files: `slice_id -> (status, title)`."""
    from scripts.dependencies import resolve_document

    def resolve(slice_id: str):
        found = resolve_document(slice_id, search_dirs, exclude=exclude)
        if found is None:
            return None, None
        data = parse_frontmatter(found.read_text(encoding="utf-8"))
        return data.get("status", "UNKNOWN"), data.get("title")

    return resolve


def sync_file(path: Path) -> tuple[int, int]:
    """Rewrite a brief's track state in place. Returns `(closed, total)`."""
    path = Path(path)
    _frontmatter, text = load(path)
    resolve = slice_resolver(search_dirs_for(path), exclude=path)
    updated = sync_text(text, resolve)
    if updated != text:
        atomic_write_text(path, updated)
    return progress(updated, resolve)
