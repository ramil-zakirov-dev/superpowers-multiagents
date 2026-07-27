"""Slice dependency checking."""

from pathlib import Path

from scripts.frontmatter import parse_frontmatter


def _candidate_dirs(spec_file: Path) -> list[Path]:
    """Directories to search: the file's own, plus its sibling specs/plans."""
    own = spec_file.parent
    dirs = [own]
    for sibling in ("specs", "plans", "milestones"):
        candidate = own.parent / sibling
        if candidate.is_dir() and candidate != own:
            dirs.append(candidate)
    return dirs


def _resolve(dep_id: str, search_dirs: list[Path], exclude: Path) -> tuple[list[Path], list[Path]]:
    """Return (matches by slice_id, matches by filename stem)."""
    by_slice_id: list[Path] = []
    by_stem: list[Path] = []
    for directory in search_dirs:
        for candidate in sorted(directory.glob("*.md")):
            if candidate.resolve() == exclude.resolve():
                continue
            frontmatter = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if frontmatter.get("slice_id") == dep_id:
                by_slice_id.append(candidate)
            elif dep_id in candidate.stem:
                by_stem.append(candidate)
    return by_slice_id, by_stem


def check_unmet_dependencies(spec_file: Path, search_dirs: list[Path] | None = None) -> list:
    """List dependencies of a slice that are not yet VERIFIED_CLOSED.

    A frontmatter `slice_id` match always wins over a filename match. Several
    equally good candidates are reported as ambiguous rather than guessed —
    silently picking one is how a dependency gate stops meaning anything.
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
        by_slice_id, by_stem = _resolve(dep_id, dirs, exclude=spec_file)
        matches = by_slice_id or by_stem

        if not matches:
            unmet.append(f"{dep_id} (spec not found in {[str(d) for d in dirs]})")
            continue
        if len(matches) > 1:
            names = sorted(match.name for match in matches)
            unmet.append(f"{dep_id} (ambiguous: matches {names})")
            continue

        dep_status = parse_frontmatter(matches[0].read_text(encoding="utf-8")).get(
            "status", "UNKNOWN"
        )
        if dep_status != "VERIFIED_CLOSED":
            unmet.append(f"{dep_id} (status: {dep_status})")

    return unmet
