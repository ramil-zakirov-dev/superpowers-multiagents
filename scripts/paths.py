"""Filesystem layout for orchestrator runtime artifacts.

Everything the orchestrator writes at runtime lives under
`<project_root>/.superpowers/`, so one ignore rule covers it all. Paths are
always derived from an explicit project root and never from the current
working directory: the executor runs with `cwd=<worktree>`, so a relative
path would split `mkdir` from its redirect target.
"""

from pathlib import Path

SUPERPOWERS_DIRNAME = ".superpowers"

#: Paths, relative to the project root, that the orchestrator itself creates.
#: `git_ops.check_working_tree_clean` ignores these when deciding cleanliness,
#: so the orchestrator's own artifacts cannot block its own merge.
ARTIFACT_PREFIXES: tuple[str, ...] = (
    ".superpowers/logs/",
    ".superpowers/locks/",
    ".worktrees/",
)


def superpowers_dir(project_root: Path) -> Path:
    return Path(project_root) / SUPERPOWERS_DIRNAME


def logs_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "logs"


def log_path(project_root: Path, role: str, stem: str) -> Path:
    return logs_dir(project_root) / f"{role}_{stem}.log"


def locks_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "locks"


def lock_path(project_root: Path, slice_id: str) -> Path:
    return locks_dir(project_root) / f"{slice_id}.lock"


def is_artifact_path(rel_path: str) -> bool:
    """True if a repo-relative path is an orchestrator runtime artifact.

    Note the explicit `./` handling: `str.lstrip("./")` would strip the
    leading dot of `.superpowers/` and silently stop matching.

    When git reports an untracked directory like `.superpowers/`, this function
    recognizes it as an artifact if it's a parent directory of known artifact paths.
    """
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]

    # Direct match: path is a known artifact prefix
    if any(normalized.startswith(prefix) for prefix in ARTIFACT_PREFIXES):
        return True

    # Parent directory match: git reported the parent, check if it contains artifacts
    # e.g., git reports "?? .superpowers/" but we know ".superpowers/logs/" is an artifact
    normalized_with_slash = normalized if normalized.endswith("/") else normalized + "/"
    return any(prefix.startswith(normalized_with_slash) for prefix in ARTIFACT_PREFIXES)
