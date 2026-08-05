"""Exception hierarchy for the orchestrator.

Library modules raise these. Only the process boundaries — orchestrator.py
and runner.py — translate them into exit codes. This is what makes the
failure paths testable.
"""


class OrchestratorError(Exception):
    """Base class for every orchestrator failure."""


class ConfigError(OrchestratorError):
    """Invalid or unusable agent configuration."""


class LockError(OrchestratorError):
    """A slice lock could not be acquired."""


class GitError(OrchestratorError):
    """A git operation failed."""


class HookError(OrchestratorError):
    """An infrastructure hook failed."""


class ValidationError(OrchestratorError):
    """A user-supplied identifier failed validation."""


class SandboxError(OrchestratorError):
    """The infrastructure sandbox could not be brought to the requested state."""


class ProvisionError(OrchestratorError):
    """A worktree could not be given a file it was declared to need."""
