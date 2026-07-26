"""Agent configuration loading and defaults.

Reads .superpowers/agents.yaml from the target project, merging it with
the hardcoded DEFAULT_CONFIG. If no config file exists, the defaults
provide full backward compatibility: OpenCode harness, opencode-go
provider, Kimi K3 planner, Minimax M3 executor.
"""

import copy
import logging
from pathlib import Path

from ruamel.yaml import YAML

from scripts.errors import ConfigError
from scripts.utils import _to_plain_dict

logger = logging.getLogger("orchestrator")

DEFAULT_CONFIG = {
    "harness": {
        "default": "opencode",
        "provider": "opencode-go"
    },
    "state_machine": {
        "valid_statuses": [
            "DRAFT_SPEC", "SPEC_APPROVED", "PLANNING", "PLAN_GENERATED",
            "PLAN_APPROVED", "EXECUTING", "EXECUTION_COMPLETE",
            "MERGE_CONFLICT", "VERIFIED_CLOSED"
        ],
        "transitions": {
            "DRAFT_SPEC": ["SPEC_APPROVED"],
            "SPEC_APPROVED": ["PLANNING", "DRAFT_SPEC"],
            "PLANNING": ["PLAN_GENERATED"],
            "PLAN_GENERATED": ["PLAN_APPROVED", "PLANNING"],
            "PLAN_APPROVED": ["EXECUTING", "PLAN_GENERATED"],
            "EXECUTING": ["EXECUTION_COMPLETE", "MERGE_CONFLICT"],
            "EXECUTION_COMPLETE": ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"],
            "VERIFIED_CLOSED": [],
            "MERGE_CONFLICT": ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]
        }
    },
    "agents": {
        "planner": {
            "model": "kimi-k3",
            "harness": "opencode",
            "provider": "opencode-go",
            "allowed_statuses": ["SPEC_APPROVED"],
            "in_progress_status": "PLANNING",
            "isolated_worktree": False,
            "prompt_template": "Read spec at {file} and create detailed TDD implementation plan using writing-plans skill. Save to docs/superpowers/plans/",
            "extra_args": []
        },
        "executor": {
            "model": "minimax-m3",
            "harness": "opencode",
            "provider": "opencode-go",
            "allowed_statuses": ["PLAN_APPROVED"],
            "in_progress_status": "EXECUTING",
            "isolated_worktree": True,
            "prompt_template": "Execute implementation plan at {file} using TDD subagent execution. Check off tasks in plan as completed.",
            "extra_args": []
        }
    }
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`.

    Mappings merge key by key so that a partial override inherits the rest of
    the defaults. Scalars and lists are replaced wholesale — a user who lists
    `allowed_statuses` means exactly that list, not an addition to ours.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_agent_config(project_root: Path) -> dict:
    """Load `.superpowers/agents.yaml`, deep-merged over DEFAULT_CONFIG.

    Raises ConfigError if the file exists but cannot be parsed: a config we
    cannot read is not a reason to silently run with different settings.
    """
    config_file = Path(project_root) / ".superpowers" / "agents.yaml"
    if not config_file.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        yaml = YAML(typ="rt")
        parsed = yaml.load(config_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to parse {config_file}: {exc}") from exc

    return deep_merge(DEFAULT_CONFIG, _to_plain_dict(parsed))


def resolve_agent(config: dict, role: str) -> dict:
    """Return a copy of an agent's config with global harness defaults applied."""
    agents = config.get("agents") or {}
    if role not in agents:
        raise ConfigError(
            f"Agent role '{role}' is not defined in the configuration. "
            f"Defined roles: {sorted(agents)}"
        )
    agent = copy.deepcopy(agents[role])
    harness = config.get("harness") or {}
    if "harness" not in agent and harness.get("default"):
        agent["harness"] = harness["default"]
    if "provider" not in agent and harness.get("provider"):
        agent["provider"] = harness["provider"]
    return agent
