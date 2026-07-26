"""Base class for all harness adapters."""


class HarnessAdapter:
    """Abstract base for CLI harness adapters.

    Every adapter translates an agent configuration dict and a task prompt
    into a shell command string that the orchestrator can execute.
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> str:
        """Build the CLI command string for the given agent and prompt.

        Args:
            agent_config: Agent configuration dict from agents.yaml.
            task_prompt: The fully-rendered task prompt.

        Returns:
            A shell command string ready for subprocess execution.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_command()"
        )
