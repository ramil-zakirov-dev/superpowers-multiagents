"""The document a dispatched role is expected to leave behind.

A role that produces a document produces it for the state machine, not only for
the human who reads it. `cmd_set_status` refuses a transition on frontmatter it
cannot parse, and `dependencies.resolve_document` finds a slice's documents by
`slice_id` — so a plan with no frontmatter is a good plan the pipeline cannot
see. Observed on a real dispatch: a 664-line plan whose first line was its H1,
which nothing caught until the next human gate.

Two halves of one contract live here. `frontmatter_block` renders what the
produced document must open with, so the dispatch prompt can state it verbatim
instead of hoping the role infers it. `find_document` asks afterwards whether
such a document exists, so the supervisor can fail the run at the moment of
production rather than one gate later — where the natural workaround is to edit
frontmatter by hand, which the pipeline's own rules forbid.

There is deliberately no `kind` in the rendered block. A spec and its plan are
two documents of *one* slice; see `milestone.KNOWN_KINDS`.
"""

from pathlib import Path

from scripts.errors import ConfigError
from scripts.frontmatter import parse_frontmatter


def frontmatter_block(slice_id: str, milestone_id: str, status: str) -> str:
    """The YAML block the produced document must open with.

    `milestone_id` is omitted when the source document has none, rather than
    written empty: an empty id matches no milestone, and a key that looks set
    but resolves to nothing is worse than an absent one.
    """
    lines = ["---", f'slice_id: "{slice_id}"']
    if milestone_id:
        lines.append(f'milestone_id: "{milestone_id}"')
    lines.append(f"status: {status}")
    lines.append("---")
    return "\n".join(lines)


def documents_dir(source_file: Path, subdir: str) -> Path:
    """The sibling directory a role's output lands in, e.g. specs/ -> plans/."""
    cleaned = (subdir or "").strip()
    if not cleaned or cleaned != Path(cleaned).name:
        raise ConfigError(
            f"`produces: {subdir!r}` must be a single directory name sitting "
            f"beside the source document's own (for example `plans`), not a path."
        )
    return Path(source_file).parent.parent / cleaned


def find_document(source_file: Path, subdir: str, slice_id: str) -> Path | None:
    """The produced document for this slice, or None when there is none.

    "None" covers every way the artifact can be invisible to the machine and
    not only a missing file: no frontmatter, a different `slice_id`, or a
    parsed block with no `status` for a gate to move. The caller reports; this
    only answers.
    """
    directory = documents_dir(source_file, subdir)
    if not directory.is_dir():
        return None
    source = Path(source_file).resolve()
    for candidate in sorted(directory.glob("*.md")):
        if candidate.resolve() == source:
            continue
        frontmatter = parse_frontmatter(candidate.read_text(encoding="utf-8"))
        if frontmatter.get("slice_id") == slice_id and frontmatter.get("status"):
            return candidate
    return None
