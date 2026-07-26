"""Slice dependency checking."""

from pathlib import Path
from scripts.frontmatter import parse_frontmatter


def check_unmet_dependencies(spec_file: Path) -> list:
    """Verifies if any slice listed in depends_on is not yet VERIFIED_CLOSED."""
    if not spec_file.exists():
        return []

    content = spec_file.read_text(encoding="utf-8")
    fm = parse_frontmatter(content)
    depends_on = fm.get("depends_on", [])

    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not depends_on:
        return []

    unmet = []
    specs_dir = spec_file.parent

    for dep_id in depends_on:
        matching = [
            f for f in specs_dir.glob("*.md")
            if dep_id in f.stem.split("-")
            or f.stem.endswith(dep_id)
            or dep_id == f.stem
        ]
        if not matching:
            fallback = list(specs_dir.glob(f"*{dep_id}*.md"))
            if len(fallback) == 1:
                matching = fallback
            else:
                unmet.append(f"{dep_id} (Spec not found)")
                continue

        dep_spec = matching[0]
        dep_fm = parse_frontmatter(dep_spec.read_text(encoding="utf-8"))
        if dep_fm.get("status", "UNKNOWN") != "VERIFIED_CLOSED":
            unmet.append(f"{dep_id} (Status: {dep_fm.get('status', 'UNKNOWN')})")

    return unmet
