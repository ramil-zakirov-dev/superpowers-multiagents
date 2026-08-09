import json
import subprocess
from pathlib import Path

import pytest

from scripts.adapters.base import HarnessAdapter
from scripts.adapters.opencode import OpenCodeAdapter
from scripts.skills import (
    compose_prompt,
    declared_instructions,
    declared_skills,
    invisible_skills,
)


def test_declared_skills_absent_is_empty():
    assert declared_skills({"model": "kimi-k3"}) == []


def test_declared_skills_preserves_order_and_dedupes():
    config = {"skills": ["clean-architecture", "clean-code", "clean-architecture"]}
    assert declared_skills(config) == ["clean-architecture", "clean-code"]


def test_declared_skills_strips_whitespace():
    assert declared_skills({"skills": [" clean-code "]}) == ["clean-code"]


def test_compose_prompt_without_skills_is_unchanged():
    assert compose_prompt("Read the spec at /tmp/s.md", []) == "Read the spec at /tmp/s.md"


def test_compose_prompt_appends_one_paragraph():
    composed = compose_prompt("Read the spec.", ["clean-architecture", "clean-code"])
    assert composed == (
        "Read the spec.\n\n"
        "Use these skills where they apply: clean-architecture, clean-code."
    )


def test_declared_instructions_absent_is_empty():
    assert declared_instructions({"model": "kimi-k3"}) == ""


def test_declared_instructions_strips_surrounding_whitespace():
    assert declared_instructions({"instructions": "  Never do X.\n"}) == "Never do X."


def test_declared_instructions_treats_a_blank_value_as_absent():
    assert declared_instructions({"instructions": "   \n"}) == ""


def test_compose_prompt_without_instructions_is_unchanged():
    assert compose_prompt("Read the spec.", [], None, "") == "Read the spec."


def test_compose_prompt_carries_instructions_verbatim():
    composed = compose_prompt("Task.", [], None, "First line.\nSecond line.")
    assert "First line.\nSecond line." in composed


def test_compose_prompt_frames_instructions_as_outranking_the_environment():
    """Last position alone does not tell a role which rule wins a conflict."""
    composed = compose_prompt("Task.", [], None, "Never do X.")
    assert "precedence" in composed


def test_compose_prompt_puts_instructions_after_skills_and_lenses():
    composed = compose_prompt("Task.", ["clean-code"], ["vendor/lens#part@abc"], "Never do X.")
    assert composed.index("Use these skills") < composed.index("cites lenses")
    assert composed.index("cites lenses") < composed.index("Never do X.")


def test_invisible_skills_reports_only_the_missing_ones():
    assert invisible_skills(["a", "b"], {"a"}) == ["b"]


def test_invisible_skills_is_silent_when_the_adapter_cannot_tell():
    assert invisible_skills(["a", "b"], None) == []


def test_invisible_skills_reports_all_when_the_harness_sees_none():
    assert invisible_skills(["a", "b"], set()) == ["a", "b"]


DEBUG_SKILL_PAYLOAD = json.dumps([
    {"name": "clean-architecture", "description": "…", "location": "/p/.claude/skills/clean-architecture/SKILL.md"},
    {"name": "customize-opencode", "description": "…", "location": "<built-in>"},
])


def test_base_adapter_cannot_tell():
    assert HarnessAdapter().list_skills({}, Path(".")) is None


def _fake_run(stdout="", returncode=0, raises=None):
    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")
    return run


def test_opencode_adapter_parses_names(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=DEBUG_SKILL_PAYLOAD))
    assert OpenCodeAdapter().list_skills({}, Path(".")) == {
        "clean-architecture", "customize-opencode",
    }


def test_opencode_adapter_runs_in_the_given_cwd(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    OpenCodeAdapter().list_skills({}, Path("/work/tree"))
    assert seen["argv"][:3] == ["opencode", "debug", "skill"]
    assert str(seen["cwd"]) == str(Path("/work/tree"))


@pytest.mark.parametrize("kwargs", [
    {"returncode": 1, "stdout": ""},
    {"stdout": "not json at all"},
    {"stdout": json.dumps({"skills": []})},        # a dict, not the expected list
    {"raises": FileNotFoundError("opencode")},
    {"raises": subprocess.TimeoutExpired("opencode", 60)},
])
def test_opencode_adapter_returns_none_on_any_failure(monkeypatch, kwargs):
    monkeypatch.setattr(subprocess, "run", _fake_run(**kwargs))
    assert OpenCodeAdapter().list_skills({}, Path(".")) is None


def test_an_isolated_role_is_told_which_tree_is_its_own():
    """#22, run 2: the prompt asserted a location the harness contradicted.

    opencode reports the resolved *project* root, which for a linked worktree
    is the parent repository, while the dispatch claimed "you are already in
    the worktree". The agent spent the whole run trying to reconcile the two
    and committed nothing. Only the dispatcher knows both facts, so it states
    both.
    """
    composed = compose_prompt(
        "Do the work", [], location="C:/repo/.worktrees/slice-01"
    )

    assert "C:/repo/.worktrees/slice-01" in composed
    assert "project root" in composed


def test_a_non_isolated_role_pays_nothing_for_the_location_paragraph():
    assert compose_prompt("Do the work", []) == "Do the work"


def test_the_location_paragraph_tells_the_agent_not_to_go_looking_outside():
    """Run 1 died here: the worktree carries tracked files only, the project's
    test command named an untracked `.venv`, and the agent went to find it."""
    composed = compose_prompt("Do the work", [], location="/tree")

    assert "report what is missing" in composed


def test_instructions_still_win_the_last_word():
    """The location is a fact; instructions are a project's standing rules,
    and last position is the tie-break they were given on purpose."""
    composed = compose_prompt(
        "Do", [], instructions="Never touch master.", location="/tree"
    )

    assert composed.index("/tree") < composed.index("Never touch master.")
