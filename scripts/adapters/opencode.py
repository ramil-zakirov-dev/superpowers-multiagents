"""OpenCode CLI harness adapter.

Formats commands for the OpenCode CLI (https://github.com/opencode).
Supports model selection, provider override via extra_args, and
safe prompt escaping via json.dumps.
"""

import json
from scripts.adapters.base import HarnessAdapter


class OpenCodeAdapter(HarnessAdapter):
    """Adapter for the OpenCode CLI harness.

    Default harness for the superpowers-multiagents orchestrator.
    Generates commands in the form:
        opencode run --model <model> [extra_args...] "<prompt>"
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> str:
        model = agent_config.get("model", "kimi-k3")
        provider = agent_config.get("provider", "opencode-go")

        parts = ["opencode", "run", "--model", model]

        for arg in agent_config.get("extra_args", []):
            parts.append(arg.format(provider=provider, model=model))

        parts.append(json.dumps(task_prompt))
        return " ".join(parts)
