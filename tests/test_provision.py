"""Files an isolated agent needs that `git worktree add` will not put there.

A worktree is populated from HEAD, so a project's untracked configuration — a
`.env` above all — is simply absent from it. The agent then runs the project's
own tests against an incomplete environment and reports a failure whose message
is about something else entirely: issue #12 recorded a missing-credentials error
raised by a test that issues no request.

Every refusal below is fail-closed on one of two readings. A file that is
declared and silently absent is the failure this list exists to prevent. A file
git would not ignore is one the agent's own `git add -A` can commit onto the
slice branch. Neither is worth guessing about, and both are cheaper to refuse
before the run than to discover inside it.
"""

import subprocess

import pytest

from scripts.errors import ProvisionError
from scripts.git_ops import create_git_worktree, merge_and_cleanup_worktree
from scripts.provision import check_sources, copy_into_worktree, declared_copies


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    """A project that ignores `.env`, holding one untracked secret."""
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", ".")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    (project / ".gitignore").write_text(".env\n.worktrees/\n", encoding="utf-8")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".env").write_text("PREMIUM_KEY=dummy-not-real\n", encoding="utf-8")
    return project


def _config(*entries):
    return {"worktree": {"copy": list(entries)}}


def _worktree(repo, slice_id="slice-01"):
    return create_git_worktree(slice_id, repo)


# --- What the feature is for -------------------------------------------------


def test_a_declared_file_reaches_the_worktree(repo):
    """The whole issue in one assertion: `git worktree add` populates tracked
    files only, so without this the agent's tree has no `.env` at all."""
    worktree = _worktree(repo)

    copied = copy_into_worktree(worktree, repo, _config(".env"))

    assert copied == [".env"]
    assert (worktree / ".env").read_text(encoding="utf-8") == "PREMIUM_KEY=dummy-not-real\n"


def test_a_copied_file_is_invisible_to_the_worktrees_own_git(repo):
    """The property the ignore check exists to guarantee, asserted directly
    rather than through the check that enforces it: an agent running
    `git add -A` in its worktree must not be able to pick the secret up."""
    worktree = _worktree(repo)
    copy_into_worktree(worktree, repo, _config(".env"))

    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert status.stdout.strip() == "", (
        f"the copied file is visible to the worktree's git: {status.stdout!r}"
    )


def test_a_re_dispatch_refreshes_a_stale_copy(repo):
    """`create_git_worktree` reuses an existing worktree, so a second dispatch
    finds the previous run's copy already there. The project root's file is the
    authority; a rotated credential must not be shadowed by yesterday's."""
    worktree = _worktree(repo)
    copy_into_worktree(worktree, repo, _config(".env"))
    (repo / ".env").write_text("PREMIUM_KEY=rotated\n", encoding="utf-8")

    copy_into_worktree(worktree, repo, _config(".env"))

    assert (worktree / ".env").read_text(encoding="utf-8") == "PREMIUM_KEY=rotated\n"


def test_a_project_that_declares_nothing_gets_nothing(repo):
    """The default is empty, and an empty list must not cost a git call."""
    worktree = _worktree(repo)

    assert copy_into_worktree(worktree, repo, {}) == []
    assert not (worktree / ".env").exists()


def test_a_nested_path_gets_its_parent_directory(repo):
    """A path whose parent is not in HEAD has nowhere to land otherwise."""
    (repo / "config").mkdir()
    (repo / "config" / "local.env").write_text("X=1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".env\n.worktrees/\nconfig/local.env\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ignore the local config")
    worktree = _worktree(repo)

    copied = copy_into_worktree(worktree, repo, _config("config/local.env"))

    assert copied == ["config/local.env"]
    assert (worktree / "config" / "local.env").read_text(encoding="utf-8") == "X=1\n"


def test_the_copy_does_not_outlive_the_worktree(repo):
    """A secret copied in has to leave with the tree it was copied for.

    `git worktree remove` deletes the directory outright, so this is a property
    of git rather than of our code — which is exactly why it is pinned: the day
    cleanup stops going through `git worktree remove`, a secret starts
    surviving its slice and nothing else would notice.
    """
    worktree = _worktree(repo)
    copy_into_worktree(worktree, repo, _config(".env"))
    assert (worktree / ".env").exists()

    merge_and_cleanup_worktree("slice-01", repo, skip_merge=True)

    assert not (worktree / ".env").exists()
    assert not worktree.exists()


# --- What it refuses ---------------------------------------------------------


def test_a_missing_source_is_refused_and_named(repo):
    """The failure the list exists to prevent, applied to the list itself: a
    declared file that is not there must stop the dispatch, not be skipped."""
    with pytest.raises(ProvisionError, match="secrets.env"):
        check_sources(_config("secrets.env"), repo)


def test_a_missing_source_is_refused_before_the_worktree_is_created(repo):
    """`check_sources` is the dispatch gate, callable with no worktree at all.

    That is the point of splitting it out: the common failure — an unconfigured
    machine — is caught among the other gates, before the dispatch creates a
    branch it would then have to leave behind.
    """
    with pytest.raises(ProvisionError):
        check_sources(_config("secrets.env"), repo)

    assert not (repo / ".worktrees").exists()


def test_a_source_outside_the_project_is_refused(repo, tmp_path):
    (tmp_path / "outside.env").write_text("K=1\n", encoding="utf-8")

    with pytest.raises(ProvisionError, match="outside the project root"):
        check_sources(_config("../outside.env"), repo)


def test_an_absolute_path_outside_the_project_is_refused(repo, tmp_path):
    outside = tmp_path / "outside.env"
    outside.write_text("K=1\n", encoding="utf-8")

    with pytest.raises(ProvisionError, match="outside the project root"):
        check_sources(_config(str(outside)), repo)


def test_a_directory_is_refused(repo):
    """Naming a directory reads as "and everything under it", which is a
    different and much larger promise than this list makes."""
    (repo / "secrets").mkdir()

    with pytest.raises(ProvisionError, match="directory"):
        check_sources(_config("secrets"), repo)


def test_a_file_tracked_at_head_is_refused(repo):
    """The worktree already has HEAD's copy. Overwriting it with the working
    tree's version would hand an isolated agent uncommitted content — the one
    thing the isolation is for, and what the Worktree Gate already refuses for
    the dispatched document itself."""
    with pytest.raises(ProvisionError, match="tracked at HEAD"):
        check_sources(_config("README.md"), repo)


def test_a_destination_git_would_not_ignore_is_refused(repo):
    """The leak. An untracked file git does not ignore is one `git add -A`
    away from being committed onto the slice branch by the agent itself."""
    (repo / "creds.txt").write_text("TOKEN=1\n", encoding="utf-8")
    worktree = _worktree(repo)

    with pytest.raises(ProvisionError, match="does not ignore"):
        copy_into_worktree(worktree, repo, _config("creds.txt"))

    assert not (worktree / "creds.txt").exists(), (
        "the file was copied before the refusal, which is the leak itself"
    )


def test_an_ignore_rule_that_head_does_not_carry_does_not_count(repo):
    """The footgun worth a message of its own: a worktree checks out HEAD, so
    an ignore rule sitting uncommitted in the main tree is not in force where
    the agent runs. Refusing on the main tree's `.gitignore` would pass here
    and leak in production."""
    (repo / "creds.txt").write_text("TOKEN=1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".env\n.worktrees/\ncreds.txt\n", encoding="utf-8"
    )  # written, deliberately not committed
    worktree = _worktree(repo)

    with pytest.raises(ProvisionError) as excinfo:
        copy_into_worktree(worktree, repo, _config("creds.txt"))

    assert "HEAD" in str(excinfo.value)


def test_one_refused_entry_copies_none_of_them(repo):
    """Two phases, and this is why: a partial provisioning is a worktree whose
    environment is incomplete in a way nothing downstream can see — the same
    condition the whole feature exists to remove."""
    (repo / "creds.txt").write_text("TOKEN=1\n", encoding="utf-8")
    worktree = _worktree(repo)

    with pytest.raises(ProvisionError):
        copy_into_worktree(worktree, repo, _config(".env", "creds.txt"))

    assert not (worktree / ".env").exists()


def test_a_declared_list_is_read_off_the_config(repo):
    assert declared_copies(_config(".env", "config/local.env")) == [
        ".env",
        "config/local.env",
    ]
    assert declared_copies({}) == []
    assert declared_copies({"worktree": {}}) == []
