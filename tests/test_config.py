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


from scripts.config import KNOWN_AGENT_KEYS, validate_config


def test_failed_status_exists_and_is_reachable():
    sm = DEFAULT_CONFIG["state_machine"]
    assert "FAILED" in sm["valid_statuses"]
    assert "FAILED" in sm["transitions"]["PLANNING"]
    assert "FAILED" in sm["transitions"]["EXECUTING"]


def test_failed_returns_to_the_gate_it_came_from():
    assert set(DEFAULT_CONFIG["state_machine"]["transitions"]["FAILED"]) == {
        "SPEC_APPROVED",
        "PLAN_APPROVED",
    }


def test_agents_declare_success_status():
    agents = DEFAULT_CONFIG["agents"]
    assert agents["planner"]["success_status"] == "PLAN_GENERATED"
    assert agents["executor"]["success_status"] == "EXECUTION_COMPLETE"


def test_default_config_validates():
    validate_config(DEFAULT_CONFIG)


def test_validate_rejects_status_outside_valid_statuses(tmp_path):
    _write_config(tmp_path, "agents:\n  planner:\n    in_progress_status: NONSENSE\n")
    with pytest.raises(ConfigError, match="NONSENSE"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_unknown_agent_key(tmp_path):
    _write_config(tmp_path, "agents:\n  planner:\n    modle: typo\n")
    with pytest.raises(ConfigError, match="modle"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_unknown_transition_target(tmp_path):
    _write_config(tmp_path, "state_machine:\n  transitions:\n    DRAFT_SPEC: [NOWHERE]\n")
    with pytest.raises(ConfigError, match="NOWHERE"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_empty_valid_statuses():
    with pytest.raises(ConfigError, match="valid_statuses"):
        validate_config({"state_machine": {"valid_statuses": [], "transitions": {}}, "agents": {}})


def test_known_agent_keys_cover_the_documented_schema():
    assert "success_status" in KNOWN_AGENT_KEYS
    assert "harness_adapter" in KNOWN_AGENT_KEYS


def _with_sandbox(**overrides):
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["sandbox"].update(overrides)
    return config


def test_sandbox_is_disabled_by_default():
    assert DEFAULT_CONFIG["sandbox"]["enabled"] is False
    assert DEFAULT_CONFIG["sandbox"]["env"] == {}


def test_unknown_sandbox_key_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["compose_fiel"] = "typo.yml"
    with pytest.raises(ConfigError, match="compose_fiel"):
        validate_config(config)


def test_unknown_template_token_fails_closed():
    config = _with_sandbox(enabled=True, env={"dsn": "postgres://{IP}:5432/db"})
    with pytest.raises(ConfigError, match=r"\{IP\}"):
        validate_config(config)


def test_known_template_tokens_are_accepted():
    config = _with_sandbox(
        enabled=True, env={"dsn": "postgres://{ip}:5432/{project}"}
    )
    validate_config(config)  # must not raise


def test_teardown_mode_outside_the_enum_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["teardown"]["on_failed"] = "nuke"
    with pytest.raises(ConfigError, match="nuke"):
        validate_config(config)


def test_unknown_teardown_key_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["teardown"]["on_whatever"] = "none"
    with pytest.raises(ConfigError, match="on_whatever"):
        validate_config(config)
