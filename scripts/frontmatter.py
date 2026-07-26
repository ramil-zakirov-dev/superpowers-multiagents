"""Markdown YAML frontmatter parsing and atomic status updates."""

import io
import os
import re
import sys
import tempfile
import logging
from pathlib import Path
from ruamel.yaml import YAML

from scripts.utils import _to_plain_dict

logger = logging.getLogger("orchestrator")

# BOM-tolerant frontmatter pattern: strips optional UTF-8 BOM before ---
FRONTMATTER_PATTERN = re.compile(r"^\ufeff?---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(content: str) -> dict:
    """Parses YAML frontmatter from a Markdown string."""
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}
    try:
        yaml = YAML(typ='rt')
        data = yaml.load(match.group(1))
        return _to_plain_dict(data) if data else {}
    except Exception as e:
        logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}


def update_frontmatter_status(
    filepath: Path,
    new_status: str,
    valid_statuses: list,
    state_transitions: dict,
) -> bool:
    """Atomically updates the status field in a markdown file's YAML frontmatter.

    Uses .tmp + os.replace for crash-safe writes.
    Enforces strict state transitions.
    """
    if new_status not in valid_statuses:
        print(f"Error: Invalid status '{new_status}'. Allowed: {valid_statuses}")
        return False

    filepath = Path(filepath).resolve()
    if not filepath.exists():
        print(f"Error: File '{filepath}' does not exist.")
        return False

    try:
        content = filepath.read_text(encoding="utf-8").lstrip("\ufeff")
        match = FRONTMATTER_PATTERN.match(content)

        yaml = YAML(typ='rt')
        yaml.preserve_quotes = True

        if match:
            yaml_text = match.group(1)
            try:
                data = yaml.load(yaml_text) or {}
            except Exception:
                return False

            current_status = data.get("status", "UNKNOWN")

            if (current_status in state_transitions
                    and new_status not in state_transitions[current_status]
                    and current_status != new_status):
                print(f"Error: Invalid state transition from '{current_status}' to '{new_status}'.")
                return False

            data["status"] = new_status

            buf = io.StringIO()
            yaml.dump(data, buf)
            new_yaml_text = buf.getvalue().strip()

            new_content = f"---\n{new_yaml_text}\n---\n" + content[match.end():]
        else:
            return False

        parent_dir = filepath.parent
        with tempfile.NamedTemporaryFile(
            "w", dir=parent_dir, delete=False, encoding="utf-8"
        ) as tf:
            tf.write(new_content)
            temp_name = tf.name

        os.replace(temp_name, filepath)
        print(f"Updated {filepath.name} status -> {new_status}")
        return True
    except (IOError, OSError):
        return False
