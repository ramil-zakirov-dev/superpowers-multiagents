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
    assert manifest["version"] == "2.2.0"


def test_no_shipped_text_contains_a_template_placeholder():
    """A published plugin must not ship the scaffold's own placeholders."""
    placeholders = ("your-username", "your-org", "YOUR_NAME", "<your-")
    for filename, text in SHIPPED_TEXT.items():
        for placeholder in placeholders:
            assert placeholder not in text, (
                f"{filename} still contains the placeholder '{placeholder}'"
            )


def test_manifest_urls_are_resolvable_and_consistent():
    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    homepage, repository = manifest["homepage"], manifest["repository"]
    assert homepage == repository, "homepage and repository must name the same project"
    assert repository.startswith("https://github.com/"), repository
    owner_and_name = repository[len("https://github.com/"):].split("/")
    assert len(owner_and_name) == 2 and all(owner_and_name), (
        f"repository URL is not owner/name shaped: {repository}"
    )
    assert repository in README, "README does not point at the manifest's repository"


def test_requirements_declare_the_test_dependency():
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "ruamel.yaml" in requirements
    assert "pytest" in requirements


def test_every_canonical_hook_event_is_documented():
    """A hook event the orchestrator emits must be findable in the docs.

    `hooks.yaml` is configuration, and an event that exists but is written
    down nowhere is how the consuming project ended up keying a hook on
    `on_slice_execution_start` — a name that never fired.
    """
    documented = CONFIGURATION + README
    for event in sorted(canonical_events(DEFAULT_CONFIG["agents"])):
        assert event in documented, (
            f"hook event '{event}' is emitted by the orchestrator but appears "
            f"in neither docs/configuration.md nor README.md"
        )


def test_failed_recovery_path_is_documented():
    """FAILED is only useful if the reader knows how to leave it."""
    transitions = DEFAULT_CONFIG["state_machine"]["transitions"]["FAILED"]
    documented = README + CONFIGURATION + ARCHITECTURE
    for target in transitions:
        assert target in documented
    assert "FAILED" in README


def test_runtime_artifact_paths_are_documented():
    """dispatch prints a .gitignore hint; the docs must explain what it means."""
    from scripts.paths import ARTIFACT_PREFIXES

    documented = README + ARCHITECTURE
    for prefix in ARTIFACT_PREFIXES:
        assert prefix.rstrip("/") in documented, (
            f"runtime artifact path '{prefix}' is created by the orchestrator "
            f"but never explained to the user"
        )


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


def test_every_sandbox_config_key_is_documented():
    from scripts.config import KNOWN_SANDBOX_KEYS

    for key in sorted(KNOWN_SANDBOX_KEYS):
        assert key in CONFIGURATION, (
            f"sandbox config key '{key}' is accepted by the loader but appears "
            f"nowhere in docs/configuration.md"
        )


def test_every_teardown_mode_is_documented():
    from scripts.config import TEARDOWN_MODES

    for mode in sorted(TEARDOWN_MODES):
        assert mode in CONFIGURATION, (
            f"teardown mode '{mode}' is valid but undocumented"
        )


def test_sandbox_is_documented_as_opt_in():
    """The plugin has no docker dependency unless a project asks for one."""
    assert "opt-in" in CONFIGURATION.lower() or "opt in" in CONFIGURATION.lower()


def test_documented_sandbox_example_survives_the_real_validator():
    """A doc example that does not load is worse than no example."""
    import re as _re

    from ruamel.yaml import YAML

    from scripts.config import DEFAULT_CONFIG, deep_merge, validate_config
    from scripts.utils import _to_plain_dict

    blocks = _re.findall(r"```yaml\n(.*?)```", CONFIGURATION, _re.DOTALL)
    sandbox_blocks = [b for b in blocks if b.lstrip().startswith("sandbox:")]
    assert sandbox_blocks, "docs/configuration.md has no sandbox YAML example"

    for block in sandbox_blocks:
        parsed = _to_plain_dict(YAML(typ="rt").load(block))
        validate_config(deep_merge(DEFAULT_CONFIG, parsed))


def _documented_agent_blocks():
    """Every ```yaml block in configuration.md that declares `agents:`."""
    import re as _re

    from ruamel.yaml import YAML

    from scripts.utils import _to_plain_dict

    for block in _re.findall(r"```yaml\n(.*?)```", CONFIGURATION, _re.DOTALL):
        parsed = _to_plain_dict(YAML(typ="rt").load(block)) or {}
        if isinstance(parsed.get("agents"), dict):
            yield parsed["agents"]


def test_documented_agent_defaults_match_the_code():
    """The doc's default block is read as the defaults; drift makes it a lie.

    Roles the docs invent to illustrate a point (`reviewer`) are not in
    DEFAULT_CONFIG and are skipped — only the roles the plugin actually
    ships are held to equality.
    """
    checked = 0
    for agents in _documented_agent_blocks():
        for role, documented in agents.items():
            if role not in DEFAULT_CONFIG["agents"]:
                continue
            actual = DEFAULT_CONFIG["agents"][role]
            for key, value in documented.items():
                assert actual.get(key) == value, (
                    f"docs/configuration.md documents {role}.{key} as {value!r}, "
                    f"but DEFAULT_CONFIG has {actual.get(key)!r}"
                )
                checked += 1
    assert checked, "no shipped role found in any documented agents block"


#: Skills the default prompt templates instruct a dispatched agent to use.
#: They ship with obra/superpowers, not with this plugin, so naming one is a
#: dependency claim: the harness must carry that skill or the instruction is
#: a no-op the agent silently works around.
PROMPTED_SKILLS = frozenset({"writing-plans", "subagent-driven-development"})


def test_default_prompts_name_their_skills_precisely():
    """A vague paraphrase does not load a skill; the skill's name does.

    The executor's template said "using TDD subagent execution" — prose that
    names no skill — while the planner's named `writing-plans` outright. The
    asymmetry meant only one of the two roles was actually being pointed at
    the discipline the workflow assumes.
    """
    templates = " ".join(
        agent.get("prompt_template", "") for agent in DEFAULT_CONFIG["agents"].values()
    )
    for skill in sorted(PROMPTED_SKILLS):
        assert skill in templates, (
            f"no default prompt_template names the '{skill}' skill"
        )


def test_prompted_skill_dependency_is_declared_in_the_docs():
    """An undeclared dependency is invisible until an agent silently ignores it.

    The default prompts only work on a harness that carries the superpowers
    skills. This plugin cannot install them and cannot detect their absence,
    so the requirement has to be written down where an installing user reads.
    """
    documented = README + CONFIGURATION
    for skill in sorted(PROMPTED_SKILLS):
        assert skill in documented, (
            f"default prompts instruct agents to use the '{skill}' skill, but "
            f"neither README.md nor docs/configuration.md says it is required"
        )
    assert "superpowers" in documented.lower()


def test_self_reported_blockage_seam_is_documented():
    """Exit code and the agent's own verdict can disagree; say which wins.

    An executor that stops mid-plan and writes its own BLOCKED record still
    exits 0 on most harnesses, so the supervisor records the success status
    over a half-finished plan. That is by design — the exit code is the only
    signal the orchestrator can trust — but a reader who does not know it
    will read EXECUTION_COMPLETE as "the plan is done".
    """
    assert "BLOCKED" in ARCHITECTURE, (
        "architecture.md never explains what happens when an agent declares "
        "itself blocked but its process exits 0"
    )


def test_hook_ordering_change_is_recorded():
    """A published contract that changed must say so somewhere a reader looks."""
    documented = ARCHITECTURE + CONFIGURATION + README
    assert "2.1.0" in documented
    assert "after" in ARCHITECTURE and "worktree" in ARCHITECTURE


def test_every_milestone_status_is_documented():
    from scripts.milestone import MILESTONE_STATUSES

    documented = README + CONFIGURATION + ARCHITECTURE + SKILL
    for status in MILESTONE_STATUSES:
        assert status in documented, f"milestone status '{status}' is undocumented"


def test_every_milestone_subcommand_is_documented():
    for action in ("milestone new", "milestone sync", "milestone check"):
        assert action in SKILL, f"'{action}' is not shown in SKILL.md"


def test_the_documented_required_sections_match_the_code():
    """A brief's shape is a contract; two copies of it must not drift."""
    from scripts.milestone import REQUIRED_SECTIONS

    for section in REQUIRED_SECTIONS:
        assert section in CONFIGURATION, (
            f"required section '{section}' is enforced by the code but appears "
            f"nowhere in docs/configuration.md"
        )


def test_the_track_markers_are_documented_verbatim():
    from scripts.milestone import TRACKS_BEGIN, TRACKS_END

    assert TRACKS_BEGIN in CONFIGURATION and TRACKS_END in CONFIGURATION


def test_the_sandbox_flags_first_warning_is_not_copied_onto_milestone():
    """That constraint exists only because `sandbox exec --` needs REMAINDER."""
    for line in SKILL.split("\n"):
        if "must precede the action" in line.lower():
            assert "sandbox" in line.lower(), (
                "the flags-first warning was generalised beyond sandbox; "
                "milestone subcommands take flags after the action"
            )


def test_the_milestone_lifecycle_is_documented_as_fixed():
    assert "not configurable" in ARCHITECTURE.lower()


def test_the_operating_procedure_states_who_decides():
    """The distinction the section exists for: the human decides, never types."""
    assert "Operating procedure" in SKILL
    assert "decides" in SKILL


def test_package_json_version_matches_plugin_manifest():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert plugin["version"] == package["version"] == "2.2.0"
