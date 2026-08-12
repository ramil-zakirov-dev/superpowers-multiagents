import json
import re
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
MARKETPLACE = (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")

#: Everything this plugin ships to a user's machine.
SHIPPED_TEXT = {
    "README.md": README,
    "skills/multiagent-orchestrator/SKILL.md": SKILL,
    "docs/configuration.md": CONFIGURATION,
    "docs/architecture.md": ARCHITECTURE,
    ".claude-plugin/plugin.json": PLUGIN_MANIFEST,
    ".claude-plugin/marketplace.json": MARKETPLACE,
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
    assert manifest["version"] == "2.20.0"


def test_no_command_file_asserts_which_roles_a_project_has():
    """`commands/dispatch.md` carried the literal

        Configured roles: `planner`, `executor`.

    which no project's config was ever consulted for. It is the most-read
    sentence about role configuration in the system — injected whenever the
    dispatch skill loads — and the only one that never read the configuration.

    2.20.0 made `<role>: null` remove a role for real, so a project can now
    subtract one and still be greeted by a command insisting it has it: the
    role leaves the machine and stays in the interface, which is the defect
    #32 was opened about, one layer up.

    The guard is over the *claim*, not the word. Command files may name a role
    in an example; what they may not do is state, as fact, which roles a
    project has. `config.resolve_agent` already names the real ones in its
    refusal, and unlike this file it cannot be wrong.
    """
    for path in sorted((REPO_ROOT / "commands").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for claim in ("Configured roles:", "Available roles:", "Defined roles:"):
            assert claim not in text, (
                f"{path.name} states which roles exist; only "
                f".superpowers/agents.yaml knows that"
            )


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


def test_every_known_agent_key_is_documented():
    from scripts.config import KNOWN_AGENT_KEYS

    undocumented = sorted(key for key in KNOWN_AGENT_KEYS if f"`{key}`" not in CONFIGURATION)
    assert not undocumented, (
        "these agent keys exist in the schema but appear nowhere in "
        f"configuration.md: {undocumented}"
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

    Three kinds of block are not that, and are skipped. Roles the docs invent
    to illustrate a point (`reviewer`) are not in DEFAULT_CONFIG. A key the
    plugin ships no default for — `skills`, `instructions` — can only be an
    example of a project override, since there is no default for it to
    contradict; holding those to equality would forbid the docs from showing a
    shipped role being configured at all. And `planner: null`, the example of
    *removing* a shipped role, claims nothing about that role's defaults.

    That last case arrived by crashing this test with
    `AttributeError: 'NoneType' object has no attribute 'items'` — the same
    assumption the slice was fixing three modules away, that an agent entry is
    always a mapping. A guard is not exempt from the bug it guards against.
    """
    checked = 0
    for agents in _documented_agent_blocks():
        for role, documented in agents.items():
            if role not in DEFAULT_CONFIG["agents"]:
                continue
            if not isinstance(documented, dict):
                continue
            actual = DEFAULT_CONFIG["agents"][role]
            for key, value in documented.items():
                if key not in actual:
                    continue
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


def test_skill_does_not_instruct_a_manual_checkbox_edit():
    """The auto-sync (§3.4) retired the manual step; the prose must not revive it.

    SKILL.md once told the human to "check off `[x]`" in the milestone brief
    by hand, directly contradicting the Operating procedure table added later
    in the same file, which says the same `set-status` command re-syncs every
    brief listing the slice. That instruction is exactly the defect this
    slice exists to retire.
    """
    lowered = SKILL.lower()
    assert "check off `[x]`" not in lowered
    assert "check off manually" not in lowered


def test_package_json_version_matches_plugin_manifest():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert plugin["version"] == package["version"] == "2.20.0"


#: Categories the official marketplace actually uses. Hardcoded because a test
#: may not reach the network; this catches a typo like "developement", not a
#: philosophical disagreement about taxonomy.
MARKETPLACE_CATEGORIES = frozenset({
    "automation", "database", "deployment", "design", "development",
    "learning", "location", "math", "monitoring", "productivity",
    "security", "testing",
})


def _marketplace():
    return json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )


def _manifest():
    return json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )


def test_marketplace_entry_matches_the_plugin_manifest():
    """Two copies of the plugin's identity must not drift apart."""
    entries = _marketplace()["plugins"]
    assert len(entries) == 1, "this repository publishes exactly one plugin"
    entry, manifest = entries[0], _manifest()
    assert entry["name"] == manifest["name"]
    assert entry["description"] == manifest["description"]


def test_marketplace_source_clones_the_manifest_repository_over_https():
    """A source pointing somewhere else installs someone else's code.

    The URL must be spelled out. A `github` source resolves to git@github.com
    unless CLAUDE_CODE_PLUGIN_PREFER_HTTPS is set, and the installer clones
    non-interactively — so every machine without a GitHub SSH key fails to
    install a public plugin.
    """
    entry = _marketplace()["plugins"][0]
    repository = _manifest()["repository"]
    assert repository.startswith("https://github.com/"), repository
    expected = repository.removesuffix(".git") + ".git"
    assert entry["source"] == {"source": "url", "url": expected}


def test_marketplace_category_is_a_real_one():
    assert _marketplace()["plugins"][0]["category"] in MARKETPLACE_CATEGORIES


def test_marketplace_is_not_named_after_the_plugin():
    """Installation reads `<plugin>@<marketplace>`; equal names read as a typo."""
    marketplace = _marketplace()
    assert marketplace["name"] != marketplace["plugins"][0]["name"]


def test_readme_documents_the_marketplace_installation_route():
    """A source checkout is not an installation, and README used to say it was."""
    assert "marketplace" in README.lower()
    assert "plugin install" in README


COMMANDS_DIR = REPO_ROOT / "commands"


def command_files():
    """Every plugin command, sorted so failures name files in a stable order."""
    return sorted(COMMANDS_DIR.glob("*.md"), key=lambda p: p.name)


#: The 8 plugin commands also ship to a user's machine, so the placeholder
#: and phantom-harness guards over SHIPPED_TEXT must see them too.
SHIPPED_TEXT.update({
    f"commands/{path.name}": path.read_text(encoding="utf-8")
    for path in command_files()
})


def command_frontmatter(path):
    """The command's YAML frontmatter as a flat mapping.

    Deliberately hand-rolled rather than routed through scripts.frontmatter:
    that module parses *lifecycle documents* and is entitled to assume a
    `status` field. A command file has none, and a shared parser would grow a
    branch for a file kind it otherwise knows nothing about.

    Also deliberately not a real YAML parse: `argument-hint: [role] [file]`
    is the documented plugin convention (plugin-dev/command-development's own
    examples use it), but a strict YAML parser reads `[role] [file]` as a
    malformed flow sequence and rejects it.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    _, body = text.split("---\n", 1)
    block, _, _ = body.partition("\n---\n")
    fields = {}
    for line in block.split("\n"):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"{path.name}: frontmatter line is not `key: value`: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def test_every_command_declares_a_description():
    """The description is what a person sees when choosing a command."""
    assert command_files(), "no commands were found"
    for path in command_files():
        fields = command_frontmatter(path)
        assert fields.get("description"), f"{path.name} has no description"


def test_every_command_that_takes_arguments_hints_them():
    for path in command_files():
        body = path.read_text(encoding="utf-8")
        takes_arguments = "$1" in body or "$ARGUMENTS" in body
        if takes_arguments:
            assert "argument-hint" in command_frontmatter(path), (
                f"{path.name} substitutes arguments but declares no argument-hint"
            )


def test_every_command_reaches_the_orchestrator_through_the_plugin_root():
    """The whole point of the command layer: nobody computes this path.

    A literal path, a `~`, or a relative traversal would reintroduce exactly
    the failure this slice removes — and would work on the author's machine.
    """
    for path in command_files():
        body = path.read_text(encoding="utf-8")
        if "orchestrator.py" not in body:
            continue
        assert '"${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py"' in body, (
            f"{path.name} runs the orchestrator by some other path"
        )
        for forbidden in ("~/", "../scripts", "C:\\", "/home/"):
            assert forbidden not in body, (
                f"{path.name} contains the non-portable path fragment {forbidden!r}"
            )


def test_every_status_a_command_sets_is_a_real_status():
    """A typo'd status must fail here, not at the gate in front of the human."""
    import re

    from scripts.config import DEFAULT_CONFIG
    from scripts.milestone import MILESTONE_STATUSES

    known = set(DEFAULT_CONFIG["state_machine"]["valid_statuses"]) | set(MILESTONE_STATUSES)
    found = set()
    for path in command_files():
        found |= set(re.findall(r"--status ([A-Z_]+)", path.read_text(encoding="utf-8")))
    assert found, "no command sets a status"
    assert found <= known, f"unknown statuses in commands/: {sorted(found - known)}"


def test_dispatch_command_sends_the_reader_to_the_config_for_its_roles():
    """This replaces a guard that checked the wrong thing, which is worth
    recording because the wrong thing looked exactly like the right one.

    The old test pinned `Configured roles: planner, executor` against
    `DEFAULT_CONFIG` and passed for as long as the file existed. What it
    proved was that the sentence matched the *plugin's defaults* — which is
    precisely the claim that is false in any project that overrides `agents:`,
    and every project that uses this plugin overrides `agents:`. A green test
    over a sentence nobody could keep true is how the line survived to be
    injected into every dispatch (#36).

    So the checkable claim is now the opposite one: the file points at the
    configuration instead of restating it.
    """
    body = (COMMANDS_DIR / "dispatch.md").read_text(encoding="utf-8")
    assert "agents.yaml" in body, (
        "dispatch.md must say where roles are defined, since it no longer "
        "claims to know them"
    )


def test_new_milestone_splits_its_arguments_in_bash():
    """The id/title split must not depend on undocumented quote handling.

    `$1`/`$2` splitting is documented by example only; nothing states what
    happens to a quoted multi-word argument. `$ARGUMENTS` is documented as the
    whole string, and `cut` has defined semantics, so the split happens there.
    """
    body = (COMMANDS_DIR / "new-milestone.md").read_text(encoding="utf-8")
    assert "$ARGUMENTS" in body, "new-milestone must take the whole argument string"
    assert "$1" not in body, "positional splitting would break a multi-word title"
    assert "cut -d' ' -f1" in body, "the id is the first word"
    assert "cut -s -d' ' -f2-" in body, (
        "the title is everything after the id, and -s is load-bearing: without "
        "it `cut` returns the whole line when there is no delimiter, so "
        "`new-milestone milestone-2` would title the brief 'milestone-2' — a "
        "plausible-looking fabrication in a tracked document rather than a "
        "visibly empty field"
    )


def test_no_command_takes_its_arguments_positionally():
    """What `new-milestone`'s comment says, applied to every command.

    Observed with plugin 2.5.0 on Claude Code: `activate-milestone <path>` ran
    with `$1` empty, so `--file` resolved to the repository root and the
    orchestrator read a directory. `dispatch <role> <path>` put the path in
    `--role`. `new-milestone` was unaffected — it is the one command that was
    already built on `$ARGUMENTS`.

    A wrapper failing here is not a cosmetic bug: these commands *are* the
    lifecycle gates, and an operator whose gate command fails is one keystroke
    from editing `status:` by hand, which is what the state machine exists to
    prevent and which leaves no trace.
    """
    offenders = [
        path.name
        for path in command_files()
        if re.search(r'"?\$[12]"?', path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"these commands take arguments positionally: {offenders}. Use "
        f"$ARGUMENTS — whole and quoted when the command takes one argument, "
        f"split with `cut` when it takes two."
    )


def test_every_command_that_takes_arguments_reads_them_from_arguments():
    """The other half: a command with an argument-hint must consume something."""
    for path in command_files():
        frontmatter = command_frontmatter(path)
        if "argument-hint" not in frontmatter:
            continue
        body = path.read_text(encoding="utf-8")
        assert "$ARGUMENTS" in body, (
            f"{path.name} hints arguments ({frontmatter['argument-hint']}) but "
            f"never reads $ARGUMENTS"
        )


def test_every_command_declares_the_tools_its_pipeline_needs():
    """A command that splits `$ARGUMENTS` runs more than a bare `python` call.

    Was specific to `new-milestone`, the only command that split its arguments
    at the time. Two more do now, and an undeclared `cut` is a permission
    refusal at the moment a lifecycle gate is being crossed.
    """
    for path in command_files():
        body = path.read_text(encoding="utf-8")
        tools = command_frontmatter(path).get("allowed-tools", "")
        for binary in ("python", "echo", "cut"):
            if f"{binary} " in body or f'"{binary}' in body:
                assert f"Bash({binary}:*)" in tools, (
                    f"{path.name} runs `{binary}` but does not declare it"
                )


COMMAND_PREFIX = "/superpowers-multiagents:"


def procedure_action_cells():
    """The third column of every data row of the Operating procedure table.

    The table is found by its header row rather than by line number so that
    editing the prose above it does not silently empty this list.
    """
    lines = SKILL.split("\n")
    header = "| When | Who decides | Run this |"
    assert header in lines, "the Operating procedure table header has changed"
    start = lines.index(header) + 2  # skip the header and the `| :--- |` row
    cells = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells.append([part.strip() for part in line.strip("|").split("|")][2])
    assert cells, "the Operating procedure table has no rows"
    return cells


def test_every_procedure_row_is_a_command_or_a_supervisor_note():
    """A row that is neither is a row someone must hand-assemble to run.

    Eight rows used to carry a bare subcommand: not runnable until the reader
    prepended `python` and a path the skill asks the model to derive. This
    assertion is positive on purpose — checking for the *absence* of subcommand
    names is defeated by `status` being a substring of `--status`.
    """
    for cell in procedure_action_cells():
        is_command = cell.startswith(f"`{COMMAND_PREFIX}")
        is_note = cell.startswith("(")
        assert is_command or is_note, (
            f"procedure row action is neither a command nor a note: {cell!r}"
        )


def test_every_procedure_command_exists_as_a_file():
    named = set()
    for cell in procedure_action_cells():
        if not cell.startswith(f"`{COMMAND_PREFIX}"):
            continue
        named.add(cell.strip("`").removeprefix(COMMAND_PREFIX).split()[0])
    assert named, "the procedure table names no commands"
    for name in sorted(named):
        assert (COMMANDS_DIR / f"{name}.md").exists(), (
            f"the procedure names {COMMAND_PREFIX}{name}, which has no file"
        )


def test_every_command_file_is_named_by_the_procedure():
    """The reverse direction. A command nobody is told to run is dead weight."""
    named = set()
    for cell in procedure_action_cells():
        if cell.startswith(f"`{COMMAND_PREFIX}"):
            named.add(cell.strip("`").removeprefix(COMMAND_PREFIX).split()[0])
    on_disk = {path.stem for path in command_files()}
    # `status` reads state and belongs to no transition, so no row names it.
    assert on_disk - named == {"status"}, (
        f"commands not named by any procedure row: {sorted(on_disk - named - {'status'})}"
    )


def test_readme_documents_every_command():
    """A shipped command a user cannot discover may as well not exist."""
    for path in command_files():
        assert f"{COMMAND_PREFIX}{path.stem}" in README, (
            f"README does not document {COMMAND_PREFIX}{path.stem}"
        )


def test_skill_documents_every_command_in_its_own_surface_section():
    """SKILL.md's own command-surface table is the copy injected into every
    session by hooks/session-start; it must not silently fall behind README's."""
    for path in command_files():
        assert f"{COMMAND_PREFIX}{path.stem}" in SKILL, (
            f"SKILL.md does not document {COMMAND_PREFIX}{path.stem}"
        )


def test_architecture_records_the_commands_directory():
    assert "commands/" in ARCHITECTURE


def test_wait_and_reconcile_are_documented():
    """The escape hatch must be written down where an operator reads."""
    assert "reconcile" in CONFIGURATION
    assert "--wait" in CONFIGURATION
    assert "kill -0" in CONFIGURATION


def test_certify_is_documented_beside_the_outcome_it_repairs():
    """`certify` is only findable if the situation that needs it is described.

    An operator meets it holding a document at a drafting status that nothing
    will move. Documenting the command without documenting `unknown` would
    leave them reading about a repair for a state the docs never mention.
    """
    assert "certify" in CONFIGURATION
    assert "produced_status" in CONFIGURATION


def test_the_three_outcomes_are_documented():
    """The exit code stopped being the verdict, and a user cannot infer that.

    `unknown` in particular changes what an operator will see: a slice that
    stays in progress with its stack still up, which reads like a stuck
    pipeline unless it is written down as a deliberate answer.
    """
    assert "settle_window_seconds" in CONFIGURATION
    assert "observation_deadline_seconds" in CONFIGURATION
    assert "unknown" in CONFIGURATION.lower()


def test_the_wait_success_narrowing_is_documented():
    """`0` changed meaning. A caller with a script depending on the old one
    has to be able to find out why it now sees `4`."""
    assert "`4`" in CONFIGURATION


def test_the_dispatch_contract_is_documented():
    """A dispatch now records FAILED for an isolated run that left no commits.
    An operator meeting that verdict has to be able to find out why, and what
    the check does not cover — a guarantee nobody wrote down is one nobody can
    rely on or argue with.
    """
    assert "What a dispatch promises, and what it verifies" in CONFIGURATION
    assert "counting is not reviewing" in CONFIGURATION.lower()
    assert "git branch --show-current" in CONFIGURATION


def test_every_worktree_config_key_is_documented():
    from scripts.config import KNOWN_WORKTREE_KEYS

    for key in sorted(KNOWN_WORKTREE_KEYS):
        assert f"worktree.{key}" in CONFIGURATION or f"`{key}`" in CONFIGURATION, (
            f"worktree config key '{key}' is accepted by the loader but appears "
            f"nowhere in docs/configuration.md"
        )


def test_documented_worktree_example_survives_the_real_validator():
    """A doc example that does not load is worse than no example."""
    import re as _re

    from ruamel.yaml import YAML

    from scripts.config import DEFAULT_CONFIG, deep_merge, validate_config
    from scripts.utils import _to_plain_dict

    blocks = _re.findall(r"```yaml\n(.*?)```", CONFIGURATION, _re.DOTALL)
    worktree_blocks = [b for b in blocks if b.lstrip().startswith("worktree:")]
    assert worktree_blocks, "docs/configuration.md has no worktree YAML example"

    for block in worktree_blocks:
        parsed = _to_plain_dict(YAML(typ="rt").load(block))
        validate_config(deep_merge(DEFAULT_CONFIG, parsed))


def test_the_worktree_provisioning_contract_is_documented():
    """Three claims a user has to be able to rely on and argue with: that a
    refusal is a refusal, which git's `.gitignore` is consulted, and that a
    copied secret does not outlive the slice."""
    assert "Files an isolated worktree does not get" in CONFIGURATION
    assert "not in force where the agent runs" in CONFIGURATION
    assert "git worktree remove" in CONFIGURATION
    assert "worktree.copy" in README, (
        "README never tells a user the feature exists"
    )


def test_the_slice_closure_contract_is_documented():
    """Three claims: that the spec closes with the plan, that this is recorded
    rather than transitioned, and the two grounds admitted for it."""
    assert "What closing a slice writes" in CONFIGURATION
    assert "recorded rather" in CONFIGURATION.lower()
    for ground in ("Observed", "Asserted"):
        assert ground in CONFIGURATION, f"the '{ground}' ground is undocumented"


def test_the_skill_no_longer_says_a_spec_can_never_be_closed():
    """SKILL.md and close-slice.md are what a model reads at the gate itself.
    Both said VERIFIED_CLOSED "must target the plan file, not the design spec",
    which stopped being true when closure became recordable — and a stale
    instruction at a gate is worse than none, because it is obeyed.
    """
    close_slice = (COMMANDS_DIR / "close-slice.md").read_text(encoding="utf-8")
    for name, text in (("SKILL.md", SKILL), ("close-slice.md", close_slice)):
        assert "not the design spec" not in text, (
            f"{name} still forbids what the orchestrator now allows"
        )
    assert "--skip-merge" in close_slice
    assert "fourth refusal" in close_slice, (
        "close-slice.md does not tell the model what to do with a spec whose "
        "slice has no plan — the refusal it is most likely to meet first"
    )


def test_the_documented_gate_count_matches_the_code():
    """The prose states a number, and a slice that adds a gate has to move it.

    Counted from the source rather than hardcoded here: the previous slice
    added the Provision Gate and left the doc saying "four".
    """
    import re as _re

    source = (REPO_ROOT / "scripts" / "orchestrator.py").read_text(encoding="utf-8")
    gates = set(_re.findall(r"\[(\w+) Gate\]", source))
    words = {4: "four", 5: "five", 6: "six", 7: "seven"}
    assert f"**Before it starts**, {words[len(gates)]} gates run" in CONFIGURATION, (
        f"orchestrator.py has {len(gates)} gates ({sorted(gates)}), and "
        f"configuration.md states a different number"
    )
