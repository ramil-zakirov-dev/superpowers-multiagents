import json
import subprocess
from pathlib import Path

import pytest

from scripts.adapters.base import HarnessAdapter
from scripts.adapters.opencode import OpenCodeAdapter
from scripts.skills import compose_prompt, declared_skills, invisible_skills


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
