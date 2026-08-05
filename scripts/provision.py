"""Files an isolated agent needs that git will not put in its worktree.

`git worktree add` populates a tree from HEAD, so anything a project keeps
untracked is absent from it. For most untracked files that is correct and is
the point of the isolation. For a project's own configuration it is not: the
agent runs the project's tests against an environment missing the one value
that makes them work, and reports a failure whose message is about something
else entirely — issue #12 recorded a missing-credentials error raised by a test
that issues no request.

`worktree.copy` names those files. Everything here exists because copying can
lie in two directions:

* a declared file that is not there — the agent runs incomplete, and nothing
  downstream can tell that apart from a genuine failure;
* a copied file git would not ignore — the agent's own `git add -A` commits it
  onto the slice branch, which for a `.env` means a secret in the history of a
  branch that is about to be merged.

Both are refused rather than warned about. A dispatch that does not start costs
a re-run; these two cost a wrong verdict and a leaked credential respectively.

Source and destination always share a relative path. That is not laziness —
it is what makes the ignore reasoning sound. `.gitignore` matches on the path,
so a file landing where its rule expects it is covered by the same rule in both
trees, while a rename would need its own separately verified rule.
"""

import shutil
import subprocess
from pathlib import Path

from scripts.errors import ProvisionError
from scripts.git_ops import is_tracked_at_head


def declared_copies(config: dict) -> list:
    """The `worktree.copy` list, or an empty one.

    Shape — a list of non-empty strings — is already guaranteed by
    `validate_config`; what cannot be checked there is anything needing a
    project root, which is every question this module actually asks.
    """
    return list((config.get("worktree") or {}).get("copy") or [])


def check_sources(config: dict, project_root) -> list:
    """Validate every declared source. Returns them as relative posix paths.

    Relative, because that is the one form naming the same file in both trees,
    and posix because git speaks it.

    Split out from the copy so a dispatch can run it among its other gates,
    before the worktree exists. The common failure here is a machine that was
    never configured, and discovering that after `git worktree add` has created
    a branch leaves something behind for no reason at all.
    """
    project_root = Path(project_root).resolve()
    relatives = []

    for entry in declared_copies(config):
        source = (project_root / entry).resolve()
        try:
            relative = source.relative_to(project_root).as_posix()
        except ValueError:
            raise ProvisionError(
                f"worktree.copy: '{entry}' resolves to '{source}', which is "
                f"outside the project root '{project_root}'. A worktree is a "
                f"checkout of this repository and has nowhere to put a file "
                f"from anywhere else — name a path inside the project."
            ) from None

        if not source.exists():
            raise ProvisionError(
                f"worktree.copy: '{entry}' does not exist at '{source}'. A "
                f"declared file that is silently absent is the failure this "
                f"list exists to prevent: the agent would run against an "
                f"incomplete environment and report a failure about something "
                f"else. Create it, or drop it from worktree.copy."
            )

        if not source.is_file():
            raise ProvisionError(
                f"worktree.copy: '{entry}' is a directory. Only regular files "
                f"are copied — naming a directory reads as 'and everything "
                f"under it', which is a much larger promise than this list "
                f"makes. Name the files individually."
            )

        if is_tracked_at_head(source, project_root):
            raise ProvisionError(
                f"worktree.copy: '{entry}' is tracked at HEAD, so the worktree "
                f"already has HEAD's copy of it. Overwriting that with the "
                f"working tree's version would hand an isolated agent "
                f"uncommitted content, which is the one thing the isolation is "
                f"for. Commit the change instead."
            )

        relatives.append(relative)

    return relatives


def copy_into_worktree(worktree_path, project_root, config: dict) -> list:
    """Copy every declared file into `worktree_path`. Returns what was copied.

    Two phases, deliberately: every entry is checked before any is written, so
    a refusal leaves the worktree exactly as git built it. A half-provisioned
    tree is an environment incomplete in a way nothing downstream can see,
    which is the condition this module exists to remove — reproducing it while
    failing would be a poor joke.

    Copies overwrite. `create_git_worktree` reuses an existing worktree, so a
    re-dispatch finds the previous run's copy already in place; the project
    root's file is the authority, and a rotated credential must not be shadowed
    by yesterday's.
    """
    relatives = check_sources(config, project_root)
    if not relatives:
        return []

    worktree_path = Path(worktree_path).resolve()
    project_root = Path(project_root).resolve()

    for relative in relatives:
        _refuse_unless_ignored(worktree_path, relative)

    for relative in relatives:
        destination = worktree_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # copy2 rather than copy: it preserves the mode, so a `.env` kept at
        # 0600 arrives at 0600 rather than at the process umask's idea of one.
        shutil.copy2(project_root / relative, destination)

    return relatives


def _refuse_unless_ignored(worktree_path: Path, relative: str) -> None:
    """Refuse unless the worktree's own git ignores this path.

    Asked of the worktree, never of the project root. A worktree checks out
    HEAD, so its `.gitignore` is HEAD's version — an ignore rule a developer
    has written but not committed is not in force where the agent runs, and
    asking the main tree would return "ignored" for exactly the case that
    leaks.

    `check-ignore` reports a *tracked* file as not-ignored even when a pattern
    matches it. That is why tracked sources are refused earlier and for their
    own stated reason: one reaching here would earn a correct refusal with a
    misleading explanation.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "check-ignore", "-q", "--", relative],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return

    if result.returncode == 1:
        raise ProvisionError(
            f"worktree.copy: '{relative}' would land in '{worktree_path}' as a "
            f"file git does not ignore, so the agent's own `git add -A` would "
            f"commit it onto the slice branch. Add a rule covering it to the "
            f"`.gitignore` that HEAD carries — the worktree is a checkout of "
            f"HEAD, so a rule written but not committed is not in force there. "
            f"Nothing was copied."
        )

    detail = (result.stderr or result.stdout or "").strip()
    raise ProvisionError(
        f"worktree.copy: git could not decide whether '{relative}' is ignored "
        f"in '{worktree_path}' (exit {result.returncode}: "
        f"{detail or 'no output'}). Refusing to copy: whether a secret can be "
        f"committed is not a question to answer by assumption. Nothing was "
        f"copied."
    )
