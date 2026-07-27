import json
from pathlib import Path

from scripts.adapters.loader import _BUILTIN_ADAPTERS
from scripts.config import DEFAULT_CONFIG
from scripts.hooks import canonical_events

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
SKILL = (REPO_ROOT / "skills" / "multiagent-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
CONFIGURATION = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
ARCHITECTURE = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
PLUGIN_MANIFEST = (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")

#: Everything this plugin ships to a user's machine.
SHIPPED_TEXT = {
    "README.md": README,
    "skills/multiagent-orchestrator/SKILL.md": SKILL,
    "docs/configuration.md": CONFIGURATION,
    "docs/architecture.md": ARCHITECTURE,
    ".claude-plugin/plugin.json": PLUGIN_MANIFEST,
}

#: Harness names that documentation has advertised without an adapter ever
#: existing. Named explicitly because prose cannot be parsed for capability
#: claims — this is a regression guard, not a general prover.
PHANTOM_HARNESSES = frozenset({"kimicode", "mimocode"})


def test_readme_does_not_reference_invented_events():
    """README used to document an on_execution_complete the code never emits."""
    known = canonical_events(DEFAULT_CONFIG["agents"])
    assert "on_execution_complete" not in README
    for event in ("on_slice_executor_start", "on_executor_complete", "on_slice_verified_closed"):
        assert event in known


def test_readme_documents_the_failed_status():
    assert "FAILED" in README


def test_configuration_documents_success_status():
    assert "success_status" in CONFIGURATION


def test_skill_resolves_the_orchestrator_by_an_absolute_or_anchored_path():
    """A bare relative path resolves against the user's project, not the plugin."""
    assert "python scripts/orchestrator.py" not in SKILL
    assert "base directory" in SKILL.lower()


def test_plugin_manifest_has_distribution_metadata():
    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    for key in ("name", "description", "version", "author", "license", "repository"):
        assert key in manifest, f"plugin.json is missing '{key}'"
    assert manifest["version"] == "2.0.0"


def test_package_json_version_matches_plugin_manifest():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert plugin["version"] == package["version"] == "2.0.0"


def test_requirements_declare_the_test_dependency():
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "ruamel.yaml" in requirements
    assert "pytest" in requirements


def test_no_shipped_text_advertises_a_harness_without_an_adapter():
    """A harness named in shipped text must be one the loader can resolve.

    SKILL.md's frontmatter `description` advertised 'KimiCode, MimoCode' long
    after the body had been corrected. That field is what the harness matches
    a skill on and it ships to users, so a phantom name there is a capability
    claim the code refuses at runtime with ConfigError.
    """
    unresolvable = PHANTOM_HARNESSES - set(_BUILTIN_ADAPTERS)
    for filename, text in SHIPPED_TEXT.items():
        lowered = text.lower()
        for name in sorted(unresolvable):
            assert name not in lowered, (
                f"{filename} advertises harness '{name}', which has no adapter. "
                f"Built-in harnesses: {sorted(_BUILTIN_ADAPTERS)}"
            )


def test_every_builtin_harness_is_documented():
    """The converse: a harness that exists must be discoverable from the docs."""
    for name in _BUILTIN_ADAPTERS:
        assert name in CONFIGURATION.lower() or name in SKILL.lower(), (
            f"built-in harness '{name}' is not mentioned in any user-facing doc"
        )
