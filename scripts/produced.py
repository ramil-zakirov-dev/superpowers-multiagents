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


def _quote(value) -> str:
    """A double-quoted YAML scalar. Values here are ids, titles and paths."""
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_list(value) -> list:
    """`depends_on` in the shape everything downstream reads.

    `check_unmet_dependencies` tolerates a bare string, so a source document
    may legitimately carry one — but a produced document should not propagate
    a form that every reader needs a special case for.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return list(value)


def frontmatter_block(
    source: dict,
    *,
    slice_id: str,
    status: str,
    source_path: str,
    title_template: str = "",
) -> str:
    """The YAML block the produced document must open with.

    The prompt tells the role to reproduce this *exactly*, so whatever is
    absent here is absent from every generated document — this is an
    instruction, not a default a diligent agent improves on. It used to carry
    three keys against the seven every hand-written document in this
    repository has, and one of the four missing ones was load-bearing:
    `check_unmet_dependencies` reads the *dispatched* document and the
    executor is dispatched at the plan, so a plan that dropped the spec's
    `depends_on` silently stopped being held back by it.

    The division of labour is: whatever is a fact about the *slice* is
    carried forward from the source, because the source already holds it and
    asking an agent to restate a known fact only invites it to restate it
    wrongly; whatever the dispatcher knows (the source's own path) it renders;
    and `title` is derived, so "exactly this block" stays literally true and
    no placeholder has to survive a language model.

    `lenses:` is deliberately not carried. It records which ways of thinking
    a document was reasoned through, and copying the spec's list onto the plan
    would have the plan assert a use nothing observed. The dispatcher already
    puts those citations in the prompt; claiming the outcome is a different
    statement from supplying the input.

    Keys that would resolve to nothing are omitted rather than written empty —
    the rule `milestone_id` already followed, now applied to `title` too.
    """
    lines = ["---", f"slice_id: {_quote(slice_id)}"]

    milestone_id = source.get("milestone_id") or ""
    if milestone_id:
        lines.append(f"milestone_id: {_quote(milestone_id)}")

    title = source.get("title") or ""
    if title:
        rendered = title_template.format(title=title) if title_template else title
        lines.append(f"title: {_quote(rendered)}")

    lines.append(f"status: {status}")

    target_version = source.get("target_version") or ""
    if target_version:
        lines.append(f"target_version: {_quote(target_version)}")

    if source_path:
        lines.append(f"spec: {_quote(source_path)}")

    depends_on = _as_list(source.get("depends_on"))
    lines.append(
        "depends_on: [" + ", ".join(_quote(dep) for dep in depends_on) + "]"
    )

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
