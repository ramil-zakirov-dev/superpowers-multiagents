"""Base class for all harness adapters."""


class HarnessAdapter:
    """Abstract base for CLI harness adapters.

    Every adapter translates an agent configuration and a task prompt into an
    argument vector. It is a list, not a shell string: the orchestrator spawns
    agents with `shell=False`, so quoting and escaping never enter the picture
    and a prompt cannot break out of its own quotes.
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> list[str]:
        """Build the argv for the given agent and prompt.

        Args:
            agent_config: Agent configuration dict from agents.yaml.
            task_prompt: The fully-rendered task prompt.

        Returns:
            A list of strings, ready for `subprocess` with `shell=False`.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_command()"
        )
