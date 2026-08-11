"""A project has to be able to write down what it does not delegate.

Two issues, one seam: absence read as permission. A gate nobody declared
admitted every status (#31); a role nobody mentioned came back at the
plugin's default model, and the obvious way to remove it crashed (#32).

Observed against 2.19.0, by running it rather than reading it:

    deep_merge(DEFAULT_CONFIG, {"agents": {"executor": {}}})["agents"]
    # ['executor', 'planner']  -- with model 'kimi-k3', the route the
    #                             project had decided against

    validate_config(deep_merge(DEFAULT_CONFIG, {"agents": {"planner": None}}))
    # TypeError: 'NoneType' object is not iterable

    validate_config(deep_merge(DEFAULT_CONFIG, {"agents": {"planner": "kimi-k3"}}))
    # ConfigError: agent 'planner': unknown key(s) ['-', '3', 'i', 'k', 'm']

The third was reported by nobody. It is the same root as the second —
`validate_config` assumes an agent entry is a mapping — but it fails worse,
because `set()` over a string produces a fluent sentence about five keys that
do not exist instead of stopping.
"""

import pytest

from scripts import abandonment
from scripts.config import (
    DEFAULT_CONFIG,
    deep_merge,
    load_agent_config,
    resolve_agent,
    validate_config,
)
from scripts.errors import ConfigError


def _write_config(project_root, text):
    sp = project_root / ".superpowers"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "agents.yaml").write_text(text, encoding="utf-8")


# --- #32: saying no to a role the plugin ships ---


def test_a_nulled_role_is_gone(tmp_path):
    """The spelling a config author reaches for, meaning what it says.

    RFC 7386 already gives `null` this meaning in a merge patch, and it is the
    only shape that can express removal at all: an `agents:` block naming only
    the roles it wants merges key by key and restores the rest.
    """
    _write_config(tmp_path, "agents:\n  planner: null\n")
    config = load_agent_config(tmp_path)

    assert "planner" not in config["agents"]
    assert "executor" in config["agents"]


def test_a_nulled_role_leaves_a_config_that_validates(tmp_path):
    """The removal is a legal configuration, not a tolerated crash."""
    _write_config(tmp_path, "agents:\n  planner: null\n")
    validate_config(load_agent_config(tmp_path))


def test_a_nulled_role_does_not_break_the_report(tmp_path):
    """`status` runs on an unvalidated config by design. It must survive one."""
    _write_config(tmp_path, "agents:\n  planner: null\n")
    config = load_agent_config(tmp_path)

    assert abandonment.in_progress_statuses(config) == {"EXECUTING"}
    assert abandonment.success_statuses(config) == {"EXECUTION_COMPLETE"}
    assert abandonment.gate_for_in_progress(config, "EXECUTING") == "PLAN_APPROVED"


def test_dispatching_a_removed_role_says_it_was_removed(tmp_path):
    """"Not defined" reads as a typo. The distinction is worth drawing,
    because the orchestrator can observe it: a role that ships in the
    defaults and is absent here was removed on purpose — nothing else can
    produce that state.
    """
    _write_config(tmp_path, "agents:\n  planner: null\n")
    config = load_agent_config(tmp_path)

    with pytest.raises(ConfigError) as exc:
        resolve_agent(config, "planner")

    assert "planner" in str(exc.value)
    assert "removed" in str(exc.value).lower()


def test_an_unknown_role_is_still_reported_as_unknown(tmp_path):
    """The falsifier for the test above: a name nobody ever shipped must not
    be described as something this project took out.
    """
    config = load_agent_config(tmp_path)

    with pytest.raises(ConfigError) as exc:
        resolve_agent(config, "revieuwer")

    assert "removed" not in str(exc.value).lower()


def test_an_empty_mapping_still_means_the_default_role(tmp_path):
    """`null` removes; `{}` is how you say "this role, all defaults". Losing
    that distinction would make every partial override a deletion.
    """
    _write_config(tmp_path, "agents:\n  planner: {}\n")
    config = load_agent_config(tmp_path)

    assert config["agents"]["planner"]["model"] == "kimi-k3"
    assert config["agents"]["planner"]["allowed_statuses"] == ["SPEC_APPROVED"]


def test_null_removal_does_not_disturb_the_other_sections(tmp_path):
    """Every other reader spells its section `config.get(X) or {}`, so `null`
    and absent already resolved alike. The new meaning has to keep that true.
    """
    _write_config(tmp_path, "sandbox: null\nworktree: null\n")
    config = load_agent_config(tmp_path)

    assert (config.get("sandbox") or {}) == {}
    assert (config.get("worktree") or {}) == {}
    validate_config(config)


# --- #32, second half: a config typo is not a traceback ---


@pytest.mark.parametrize(
    "written, shape",
    [("kimi-k3", "str"), ("[a, b]", "list"), ("3", "int")],
    ids=["a-model-where-the-mapping-goes", "a-list", "a-number"],
)
def test_a_non_mapping_role_is_named_not_iterated(tmp_path, written, shape):
    """`set(agent)` over a string iterates characters. The message that came
    out — unknown key(s) ['-', '3', 'i', 'k', 'm'] — was not merely unhelpful,
    it was confident and wrong, which costs the reader a search.
    """
    _write_config(tmp_path, f"agents:\n  planner: {written}\n")
    config = load_agent_config(tmp_path)

    with pytest.raises(ConfigError) as exc:
        validate_config(config)

    message = str(exc.value)
    assert "planner" in message
    assert shape in message
    assert "unknown key" not in message


def test_a_malformed_role_does_not_crash_the_report(tmp_path):
    """The read path is the one place a bad config must not be fatal — the
    report exists to tell you what is on disk, and "your config is broken" is
    the least useful moment to stop saying it.
    """
    _write_config(tmp_path, "agents:\n  planner: kimi-k3\n")
    config = load_agent_config(tmp_path)

    assert abandonment.in_progress_statuses(config) == {"EXECUTING"}
    assert abandonment.success_statuses(config) == {"EXECUTION_COMPLETE"}
    assert abandonment.isolated_success_statuses(config) == {"EXECUTION_COMPLETE"}
    assert abandonment.gate_for_in_progress(config, "EXECUTING") == "PLAN_APPROVED"
    assert abandonment.certifiable_statuses(config) == {}


# --- #31: an unspoken gate is not a wide-open one ---


@pytest.mark.parametrize(
    "written",
    ["    allowed_statuses: []\n", "    allowed_statuses:\n", ""],
    ids=["empty-list", "key-with-no-value", "key-omitted"],
)
def test_a_role_without_an_entry_gate_is_refused(tmp_path, written):
    """All three spellings resolved to the same wide-open gate, and the one
    component that could have caught it — validate_config — passed them.

    The third is the one that bites a stranger: `docs/configuration.md` tells
    the reader to declare `success_status` and `in_progress_status`, and never
    says the entry gate is mandatory.
    """
    _write_config(
        tmp_path,
        "agents:\n"
        "  reviewer:\n"
        "    in_progress_status: PLANNING\n"
        "    success_status: PLAN_GENERATED\n" + written,
    )
    config = load_agent_config(tmp_path)

    with pytest.raises(ConfigError) as exc:
        validate_config(config)

    assert "reviewer" in str(exc.value)
    assert "allowed_statuses" in str(exc.value)


def test_a_partial_override_still_inherits_its_gate(tmp_path):
    """The falsifier, and the one config in this repository that the rule
    above must not break: overriding a single key of a shipped role leaves
    `allowed_statuses` inherited, not absent.
    """
    _write_config(tmp_path, "agents:\n  executor:\n    in_progress_status: WORKING\n")
    config = load_agent_config(tmp_path)
    config["state_machine"]["valid_statuses"].append("WORKING")

    validate_config(config)
    assert config["agents"]["executor"]["allowed_statuses"] == ["PLAN_APPROVED"]


def test_the_shipped_defaults_obey_the_rule_they_impose():
    """A validator whose own defaults fail it is a validator nobody keeps."""
    validate_config(DEFAULT_CONFIG)
    for role, agent in DEFAULT_CONFIG["agents"].items():
        assert agent["allowed_statuses"], f"{role} ships without an entry gate"
