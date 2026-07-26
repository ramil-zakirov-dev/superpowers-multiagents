"""Agent configuration loading and defaults.

Reads .superpowers/agents.yaml from the target project, merging it with
the hardcoded DEFAULT_CONFIG. If no config file exists, the defaults
provide full backward compatibility: OpenCode harness, opencode-go
provider, Kimi K3 planner, Minimax M3 executor.
"""

import logging
from pathlib import Path
from ruamel.yaml import YAML
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


def load_agent_config(project_root: Path) -> dict:
    """Load agent configuration from .superpowers/agents.yaml.

    Merges the project-specific config with DEFAULT_CONFIG.
    Returns a tuple of (config_dict, valid_statuses, state_transitions).
    """
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_file = project_root / ".superpowers" / "agents.yaml"

    if config_file.exists():
        try:
            yaml = YAML(typ='rt')
            parsed = yaml.load(config_file.read_text(encoding="utf-8")) or {}
            parsed_dict = _to_plain_dict(parsed)
            if "harness" in parsed_dict:
                config["harness"].update(parsed_dict["harness"])
            if "state_machine" in parsed_dict:
                config["state_machine"] = parsed_dict["state_machine"]
            if "agents" in parsed_dict:
                config["agents"].update(parsed_dict["agents"])
        except Exception as e:
            logger.warning(f"Failed to parse agents.yaml: {e}. Using defaults.")

    return config
