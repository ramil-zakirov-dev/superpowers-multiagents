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
