"""OpenCode CLI harness adapter.

Formats an argv for the OpenCode CLI. The model is passed in the CLI's native
`provider/model` form — under the default empty `extra_args` the provider was
previously dropped entirely.
"""

from scripts.adapters.base import HarnessAdapter


class OpenCodeAdapter(HarnessAdapter):
    """Adapter for the OpenCode CLI harness.

    Produces: ``opencode run --model <provider>/<model> [extra_args...] <prompt>``
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> list[str]:
        model = agent_config.get("model", "kimi-k3")
        provider = agent_config.get("provider", "opencode-go")

        argv = ["opencode", "run", "--model", f"{provider}/{model}"]
        for arg in agent_config.get("extra_args") or []:
            argv.append(str(arg).format(provider=provider, model=model))
        argv.append(task_prompt)
        return argv
