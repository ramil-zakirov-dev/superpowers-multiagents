"""Filesystem layout for orchestrator runtime artifacts.

Everything the orchestrator writes at runtime lives under
`<project_root>/.superpowers/`, so one ignore rule covers it all. Paths are
always derived from an explicit project root and never from the current
working directory: the executor runs with `cwd=<worktree>`, so a relative
path would split `mkdir` from its redirect target.
"""

from pathlib import Path

from scripts.errors import OrchestratorError

SUPERPOWERS_DIRNAME = ".superpowers"

#: Paths, relative to the project root, that the orchestrator itself creates.
#: `git_ops.check_working_tree_clean` ignores these when deciding cleanliness,
#: so the orchestrator's own artifacts cannot block its own merge.
ARTIFACT_PREFIXES: tuple[str, ...] = (
    ".superpowers/logs/",
    ".superpowers/locks/",
    ".superpowers/sandbox/",
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


def sandbox_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "sandbox"


def sandbox_state_path(project_root: Path, project_name: str) -> Path:
    """Where one compose project's allocation record lives.

    Keyed by the compose project name rather than the raw branch, because a
    branch name may contain path separators and a compose project name is
    already constrained to `[a-z0-9_-]`.
    """
    return sandbox_dir(project_root) / f"{project_name}.json"


def document_prompt_path(target_file: Path, project_root: Path) -> str:
    """The document's path as an agent should be told it, posix-flavoured.

    Relative to the project root, which is the one form that is correct in
    both trees a dispatch can put an agent in: a worktree is a checkout of
    the same repository with the same layout, so the same relative path names
    the worktree's own copy. An absolute path names the project root's copy
    only, and handing that to an isolated agent tells it to work in the tree
    it was kept out of.

    Posix separators because git speaks them, the layout is identical on
    every platform, and a backslash travelling through a prompt string into a
    CLI argument is one more thing that can be eaten in transit.

    Refuses a document outside the root: there is no correct prompt to render
    for one, and dispatching an agent at a path it cannot resolve burns a
    model's quota discovering that.
    """
    target_file = Path(target_file).resolve()
    project_root = Path(project_root).resolve()
    try:
        return target_file.relative_to(project_root).as_posix()
    except ValueError:
        raise OrchestratorError(
            f"'{target_file}' is not inside the project root '{project_root}', "
            f"so there is no path an agent working in that root could open. "
            f"Dispatch a document that lives in the project."
        ) from None


def is_artifact_path(rel_path: str) -> bool:
    """True if a repo-relative path is an orchestrator runtime artifact.

    Note the explicit `./` handling: `str.lstrip("./")` would strip the
    leading dot of `.superpowers/` and silently stop matching.

    Callers must list untracked directories with `git status
    --porcelain --untracked-files=all` so a wholly-new directory (e.g. the
    first-ever `.superpowers/logs/`) is reported as its individual files,
    not collapsed into one `?? .superpowers/` line — a bare parent
    directory can't be safely matched here without also risking a false
    negative on unrelated content that happens to share the same parent.
    """
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return any(normalized.startswith(prefix) for prefix in ARTIFACT_PREFIXES)
