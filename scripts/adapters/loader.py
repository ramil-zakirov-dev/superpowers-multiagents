"""Dynamic adapter resolution and loading.

Resolves the correct HarnessAdapter for an agent configuration:
1. If harness_adapter path is set, dynamically imports the custom module.
2. Otherwise, selects a built-in adapter by the harness name.
"""

import sys
import importlib.util
from pathlib import Path

from scripts.adapters.base import HarnessAdapter
from scripts.adapters.opencode import OpenCodeAdapter
from scripts.errors import ConfigError

# Registry of built-in adapters by harness name
_BUILTIN_ADAPTERS: dict[str, type[HarnessAdapter]] = {
    "opencode": OpenCodeAdapter,
}


def get_harness_adapter(agent_config: dict, project_root: Path) -> HarnessAdapter:
    """Resolve and instantiate the correct HarnessAdapter for an agent.

    Args:
        agent_config: Agent configuration dict (from agents.yaml).
        project_root: Absolute path to the target project root.

    Returns:
        An instantiated HarnessAdapter ready to build commands.
    """
    # Custom adapter path takes priority
    custom_path = agent_config.get("harness_adapter")
    if custom_path:
        return _load_custom_adapter(custom_path, project_root)

    # Built-in adapter lookup
    harness_type = agent_config.get("harness", "opencode").lower()
    adapter_cls = _BUILTIN_ADAPTERS.get(harness_type)
    if adapter_cls is None:
        raise ConfigError(
            f"Unknown harness type '{harness_type}'. "
            f"Available: {sorted(_BUILTIN_ADAPTERS)}"
        )

    return adapter_cls()


def _load_custom_adapter(relative_path: str, project_root: Path) -> HarnessAdapter:
    """Import a custom adapter from a project-local Python file.

    Bytecode writing is suppressed for the duration: the adapter lives in the
    user's repository, and dropping a `__pycache__/` beside it would leave
    their working tree dirty.
    """
    adapter_file = (Path(project_root) / relative_path).resolve()
    if not adapter_file.exists():
        raise ConfigError(f"Custom harness adapter not found: {adapter_file}")

    module_name = f"custom_adapter_{adapter_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, adapter_file)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Could not load custom harness adapter: {adapter_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, HarnessAdapter)
                and attr is not HarnessAdapter):
            return attr()

    raise ConfigError(f"No HarnessAdapter subclass found in {adapter_file}.")
