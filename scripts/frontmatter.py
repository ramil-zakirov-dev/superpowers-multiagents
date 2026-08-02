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

# BOM-tolerant frontmatter pattern: strips optional UTF-8 BOM before ---.
# The trailing runs match horizontal whitespace only. `\s*\n` would also eat
# the blank line that conventionally follows the closing `---`, and the writer
# below rebuilds the block from this match -- so a greedy class here deletes a
# line of the document on every status change.
FRONTMATTER_PATTERN = re.compile(r"^\ufeff?---[^\S\n]*\n(.*?)\n---[^\S\n]*\n", re.DOTALL)


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
        # Read with newline="" so the document's own convention survives the
        # round trip. read_text() would report every ending as "\n" and the
        # write below would then stamp the host's os.linesep onto every line,
        # turning a one-field edit into a whole-file diff on Windows.
        with filepath.open(encoding="utf-8", newline="") as fh:
            raw = fh.read().lstrip("\ufeff")
        newline = "\r\n" if "\r\n" in raw else "\n"
        content = raw.replace("\r\n", "\n")
        match = FRONTMATTER_PATTERN.match(content)

        yaml = YAML(typ='rt')
        yaml.preserve_quotes = True
        # Default width is 80, which folds a long title onto a continuation
        # line. Rewriting a field this call was not asked to touch is a
        # gratuitous diff, and reviewers stop reading status changes that
        # carry them.
        yaml.width = 4096

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
            "w", dir=parent_dir, delete=False, encoding="utf-8", newline=newline
        ) as tf:
            tf.write(new_content)
            temp_name = tf.name

        os.replace(temp_name, filepath)
        print(f"Updated {filepath.name} status -> {new_status}")
        return True
    except (IOError, OSError):
        return False
