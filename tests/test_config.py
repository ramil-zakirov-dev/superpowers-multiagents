import pytest

from scripts.config import DEFAULT_CONFIG, deep_merge, load_agent_config, resolve_agent
from scripts.errors import ConfigError


def _write_config(project_root, text):
    sp = project_root / ".superpowers"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "agents.yaml").write_text(text, encoding="utf-8")


def test_deep_merge_merges_mappings_key_by_key():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    assert deep_merge(base, {"a": {"y": 9}}) == {"a": {"x": 1, "y": 9}, "b": 3}


def test_deep_merge_replaces_lists_wholesale():
    assert deep_merge({"a": [1, 2, 3]}, {"a": [9]}) == {"a": [9]}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": 1}}
    deep_merge(base, {"a": {"x": 2}})
    assert base == {"a": {"x": 1}}


def test_partial_state_machine_override_inherits_valid_statuses(tmp_path):
    """Overriding only transitions must not drop valid_statuses."""
    _write_config(tmp_path, "state_machine:\n  transitions:\n    DRAFT_SPEC: [SPEC_APPROVED]\n")
    config = load_agent_config(tmp_path)
    assert config["state_machine"]["valid_statuses"] == DEFAULT_CONFIG["state_machine"]["valid_statuses"]
    assert config["state_machine"]["transitions"]["DRAFT_SPEC"] == ["SPEC_APPROVED"]


def test_partial_agent_override_keeps_the_state_gate(tmp_path):
    """Overriding only `model` must not silently disable allowed_statuses."""
    _write_config(tmp_path, "agents:\n  planner:\n    model: my-model\n")
    config = load_agent_config(tmp_path)
    planner = config["agents"]["planner"]
    assert planner["model"] == "my-model"
    assert planner["allowed_statuses"] == ["SPEC_APPROVED"]
    assert planner["in_progress_status"] == "PLANNING"
    assert "{file}" in planner["prompt_template"]


def test_global_harness_is_inherited_by_agents(tmp_path):
    _write_config(tmp_path, "harness:\n  default: myharness\n  provider: myprovider\n")
    config = load_agent_config(tmp_path)
    # The defaults declare an explicit harness, so clear it to observe inheritance.
    del config["agents"]["planner"]["harness"]
    del config["agents"]["planner"]["provider"]
    planner = resolve_agent(config, "planner")
    assert planner["harness"] == "myharness"
    assert planner["provider"] == "myprovider"


def test_explicit_agent_harness_wins_over_global(tmp_path):
    _write_config(
        tmp_path,
        "harness:\n  default: myharness\nagents:\n  planner:\n    harness: opencode\n",
    )
    config = load_agent_config(tmp_path)
    assert resolve_agent(config, "planner")["harness"] == "opencode"


def test_resolve_agent_rejects_unknown_role(tmp_path):
    config = load_agent_config(tmp_path)
    with pytest.raises(ConfigError, match="reviewer"):
        resolve_agent(config, "reviewer")


def test_malformed_yaml_fails_closed(tmp_path):
    _write_config(tmp_path, "agents: [this is: broken\n")
    with pytest.raises(ConfigError):
        load_agent_config(tmp_path)
