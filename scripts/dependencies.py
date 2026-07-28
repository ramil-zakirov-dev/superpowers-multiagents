"""Slice dependency checking."""

from pathlib import Path

from scripts.errors import ValidationError
from scripts.frontmatter import parse_frontmatter
from scripts.milestone import is_closed

#: Directory names in the order a matching document is preferred. A slice's
#: spec and its plan carry the same `slice_id`; the terminal status lands on
#: the plan, so the plan answers "is this closed?".
DIRECTORY_PRIORITY = ("plans", "specs", "milestones")


def _candidate_dirs(spec_file: Path) -> list[Path]:
    """Directories to search: the file's own, plus its sibling specs/plans."""
    own = spec_file.parent
    dirs = [own]
    for sibling in ("specs", "plans", "milestones"):
        candidate = own.parent / sibling
        if candidate.is_dir() and candidate != own:
            dirs.append(candidate)
    return dirs


def _matches(dep_id: str, search_dirs: list[Path], exclude: Path) -> list[Path]:
    """Files matching `dep_id`, by frontmatter `slice_id` first, stem second."""
    by_slice_id: list[Path] = []
    by_stem: list[Path] = []
    for directory in search_dirs:
        if not Path(directory).is_dir():
            continue
        for candidate in sorted(Path(directory).glob("*.md")):
            if candidate.resolve() == Path(exclude).resolve():
                continue
            frontmatter = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if frontmatter.get("slice_id") == dep_id:
                by_slice_id.append(candidate)
            elif dep_id in candidate.stem:
                by_stem.append(candidate)
    return by_slice_id or by_stem


def resolve_document(dep_id: str, search_dirs: list[Path], exclude: Path) -> Path | None:
    """The single document `dep_id` names, or None when nothing matches.

    A frontmatter `slice_id` match always wins over a filename match. Among
    equally good matches, `DIRECTORY_PRIORITY` decides — that is what keeps a
    slice's spec and plan from reading as a conflict. Two matches inside the
    same priority group are a real ambiguity and raise, because silently
    picking one is how a dependency gate stops meaning anything.
    """
    matches = _matches(dep_id, search_dirs, exclude)
    if not matches:
        return None

    for group in DIRECTORY_PRIORITY:
        in_group = [m for m in matches if m.parent.name == group]
        if in_group:
            matches = in_group
            break

    if len(matches) > 1:
        names = sorted(match.name for match in matches)
        raise ValidationError(f"'{dep_id}' is ambiguous: matches {names}")
    return matches[0]


def check_unmet_dependencies(spec_file: Path, search_dirs: list[Path] | None = None) -> list:
    """List dependencies of a slice that are not yet closed.

    "Closed" is asked of the resolved document's own kind: `VERIFIED_CLOSED`
    for a slice, `MILESTONE_CLOSED` for a milestone.
    """
    spec_file = Path(spec_file)
    if not spec_file.exists():
        return []

    frontmatter = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
    depends_on = frontmatter.get("depends_on", [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not depends_on:
        return []

    dirs = search_dirs if search_dirs is not None else _candidate_dirs(spec_file)

    unmet = []
    for dep_id in depends_on:
        try:
            resolved = resolve_document(dep_id, dirs, exclude=spec_file)
        except ValidationError as exc:
            unmet.append(str(exc))
            continue

        if resolved is None:
            unmet.append(f"{dep_id} (spec not found in {[str(d) for d in dirs]})")
            continue

        dep_frontmatter = parse_frontmatter(resolved.read_text(encoding="utf-8"))
        if not is_closed(dep_frontmatter):
            unmet.append(f"{dep_id} (status: {dep_frontmatter.get('status', 'UNKNOWN')})")

    return unmet
