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
        print(f"Error: Unknown harness type '{harness_type}'. "
              f"Available: {list(_BUILTIN_ADAPTERS.keys())}")
        sys.exit(1)

    return adapter_cls()


def _load_custom_adapter(relative_path: str, project_root: Path) -> HarnessAdapter:
    """Dynamically import a custom adapter from a project-local Python file."""
    adapter_file = (project_root / relative_path).resolve()
    if not adapter_file.exists():
        print(f"Error: Custom harness adapter not found: {adapter_file}")
        sys.exit(1)

    module_name = f"custom_adapter_{adapter_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, adapter_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, HarnessAdapter)
                and attr is not HarnessAdapter):
            return attr()

    print(f"Error: No HarnessAdapter subclass found in {adapter_file}.")
    sys.exit(1)
