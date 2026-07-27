from pathlib import Path

from scripts.paths import (
    ARTIFACT_PREFIXES,
    is_artifact_path,
    lock_path,
    log_path,
    logs_dir,
    locks_dir,
)


def test_log_path_is_under_superpowers():
    root = Path("/proj")
    assert log_path(root, "executor", "slice-01-plan") == (
        root / ".superpowers" / "logs" / "executor_slice-01-plan.log"
    )


def test_lock_path_is_under_superpowers():
    root = Path("/proj")
    assert lock_path(root, "slice-01") == root / ".superpowers" / "locks" / "slice-01.lock"


def test_dirs_are_derived_from_project_root_not_cwd(tmp_path):
    # tmp_path is a genuinely absolute path on the current platform;
    # a hardcoded POSIX literal like "/some/other/place" is not absolute
    # on Windows, which is what this test needs to guard against.
    root = tmp_path / "some" / "other" / "place"
    assert logs_dir(root).is_absolute()
    assert locks_dir(root).is_absolute()
    assert str(logs_dir(root)).startswith(str(root))


def test_is_artifact_path_recognises_own_artifacts():
    assert is_artifact_path(".superpowers/logs/executor_x.log")
    assert is_artifact_path(".superpowers/locks/slice-01.lock")
    assert is_artifact_path(".worktrees/slice-01/")


def test_is_artifact_path_normalises_separators_and_dot_prefix():
    assert is_artifact_path(".superpowers\\logs\\executor_x.log")
    assert is_artifact_path("./.superpowers/logs/executor_x.log")


def test_is_artifact_path_rejects_user_files():
    assert not is_artifact_path("src/main.py")
    assert not is_artifact_path(".superpowersfoo/x")
    assert not is_artifact_path("docs/superpowers/specs/design.md")


def test_artifact_prefixes_are_declared():
    assert ".superpowers/logs/" in ARTIFACT_PREFIXES
    assert ".superpowers/locks/" in ARTIFACT_PREFIXES
    assert ".worktrees/" in ARTIFACT_PREFIXES


def test_sandbox_state_lives_under_the_superpowers_root(tmp_path):
    from scripts.paths import sandbox_dir, sandbox_state_path

    assert sandbox_dir(tmp_path) == tmp_path / ".superpowers" / "sandbox"
    assert sandbox_state_path(tmp_path, "feat-alpha") == (
        tmp_path / ".superpowers" / "sandbox" / "feat-alpha.json"
    )


def test_sandbox_state_is_a_runtime_artifact():
    from scripts.paths import ARTIFACT_PREFIXES, is_artifact_path

    assert ".superpowers/sandbox/" in ARTIFACT_PREFIXES
    assert is_artifact_path(".superpowers/sandbox/feat-alpha.json")
