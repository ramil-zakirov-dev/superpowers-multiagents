"""The path the dispatch prompt hands an agent.

An absolute path names one tree — always the project root's. An isolated
role's cwd is `.worktrees/<slice_id>`, so an absolute path tells that agent
to work in the tree it was specifically kept out of. Observed: an executor
read the main tree's copy of its plan, worked there, and left seven commits
on `main` with its own `feat/` branch empty.

A worktree is a checkout of the same repository with the same layout, so one
expression is correct for both roles: the document's path relative to the
project root. That is also why the fix needs no reordering of dispatch — the
project root is known long before the worktree exists.
"""

import argparse

import pytest

from scripts.errors import OrchestratorError
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import document_prompt_path
from tests.test_dispatch_integration import (
    RECORDING_ADAPTER,
    _args,
    _wait_for,
)


def _use_recording_adapter(project_root, isolated):
    from tests.conftest import REPO_ROOT
    (project_root / "recording_adapter.py").write_text(
        RECORDING_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    harness_adapter: 'recording_adapter.py'\n"
        f"    isolated_worktree: {'true' if isolated else 'false'}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("isolated", [False, True])
def test_the_prompt_carries_a_path_relative_to_the_project_root(
    tmp_project, demo_spec, monkeypatch, tmp_path, isolated
):
    """Identical for both roles, and that identity is the point: it is the
    reason one expression serves an agent standing in the project root and an
    agent standing in a worktree.
    """
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    _use_recording_adapter(tmp_project, isolated)

    cmd_dispatch_agent(_args(demo_spec))

    prompt = prompt_log.read_text(encoding="utf-8")
    assert f"docs/superpowers/specs/{demo_spec.name}" in prompt
    assert str(tmp_project) not in prompt
    assert "\\" not in prompt.split(" using ")[0], "backslashes survive into the prompt"


def test_the_isolated_agents_path_resolves_inside_its_own_worktree(
    tmp_project, demo_spec, monkeypatch, tmp_path
):
    """The property the incident violated: the agent's cwd and the path it is
    given have to name the same file.
    """
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    _use_recording_adapter(tmp_project, isolated=True)

    cmd_dispatch_agent(_args(demo_spec))

    worktree = tmp_project / ".worktrees" / "slice-01-demo"
    assert _wait_for(lambda: worktree.exists())
    relative = f"docs/superpowers/specs/{demo_spec.name}"
    assert relative in prompt_log.read_text(encoding="utf-8")
    assert (worktree / relative).exists(), (
        "the path in the prompt does not resolve inside the agent's own cwd"
    )


# --- the precondition itself ---


def test_a_document_under_the_root_renders_relative(tmp_path):
    root = tmp_path / "project"
    document = root / "docs" / "superpowers" / "specs" / "a-design.md"
    document.parent.mkdir(parents=True)
    document.write_text("---\n---\n", encoding="utf-8")

    assert document_prompt_path(document, root) == "docs/superpowers/specs/a-design.md"


def test_a_document_outside_the_root_is_refused(tmp_path):
    """Unreachable from the CLI today — `find_project_root` walks up from the
    document, so the root is always an ancestor. The guard is here so the
    invariant is stated and stays stated: the day a caller supplies a project
    root from somewhere else, an unsatisfiable prompt is refused instead of
    rendered.
    """
    root = tmp_path / "project"
    root.mkdir()
    outsider = tmp_path / "elsewhere" / "a-design.md"
    outsider.parent.mkdir(parents=True)
    outsider.write_text("---\n---\n", encoding="utf-8")

    with pytest.raises(OrchestratorError) as excinfo:
        document_prompt_path(outsider, root)

    message = str(excinfo.value)
    assert str(outsider) in message and str(root) in message
