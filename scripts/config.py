"""Agent configuration loading and defaults.

Reads .superpowers/agents.yaml from the target project, merging it with
the hardcoded DEFAULT_CONFIG. If no config file exists, the defaults
provide full backward compatibility: OpenCode harness, opencode-go
provider, Kimi K3 planner, Minimax M3 executor.
"""

import copy
import logging
import re
from pathlib import Path

from ruamel.yaml import YAML

from scripts.errors import ConfigError
from scripts.utils import _to_plain_dict

logger = logging.getLogger("orchestrator")

DEFAULT_CONFIG = {
    "harness": {
        "default": "opencode",
        "provider": "opencode-go"
    },
    "state_machine": {
        "valid_statuses": [
            "DRAFT_SPEC", "SPEC_APPROVED", "PLANNING", "PLAN_DRAFTING",
            "PLAN_GENERATED", "PLAN_APPROVED", "EXECUTING", "EXECUTION_COMPLETE",
            "MERGE_CONFLICT", "FAILED", "VERIFIED_CLOSED"
        ],
        #: A status names where the *work* stands. Two consequences shape this
        #: table. An in-progress status can return to the gate its dispatch
        #: started from, so a run that died puts the slice back where the human
        #: left it instead of into a state they have to hand-walk out of. And a
        #: produced document is born `PLAN_DRAFTING` — the planner writes the
        #: file the moment it starts typing, so `PLAN_GENERATED` has to be
        #: something only the supervisor's epilogue can say.
        "transitions": {
            "DRAFT_SPEC": ["SPEC_APPROVED"],
            "SPEC_APPROVED": ["PLANNING", "DRAFT_SPEC"],
            "PLANNING": ["PLAN_GENERATED", "SPEC_APPROVED", "FAILED"],
            "PLAN_DRAFTING": ["PLAN_GENERATED"],
            "PLAN_GENERATED": ["PLAN_APPROVED", "PLANNING"],
            "PLAN_APPROVED": ["EXECUTING", "PLAN_GENERATED"],
            "EXECUTING": [
                "EXECUTION_COMPLETE", "MERGE_CONFLICT", "PLAN_APPROVED", "FAILED"
            ],
            "EXECUTION_COMPLETE": ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"],
            #: Nothing writes this any more. The exits stay so a document that
            #: reached it under an older version — or under a custom machine
            #: that still declares it — keeps its way back to a gate.
            "FAILED": ["SPEC_APPROVED", "PLAN_APPROVED"],
            "VERIFIED_CLOSED": [],
            "MERGE_CONFLICT": ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]
        }
    },
    "agents": {
        "planner": {
            "model": "kimi-k3",
            "harness": "opencode",
            "provider": "opencode-go",
            "allowed_statuses": ["SPEC_APPROVED"],
            "in_progress_status": "PLANNING",
            "success_status": "PLAN_GENERATED",
            "isolated_worktree": False,
            "produces": "plans",
            #: The status the produced document is *born* with, rendered into
            #: the frontmatter the prompt tells the role to reproduce. A
            #: planner following that instruction writes the file the moment it
            #: starts drafting and then works on it for minutes, so a file
            #: carrying `success_status` said "generated" for almost the whole
            #: run — byte-identical to the same file after the epilogue, and
            #: meaning the opposite. The epilogue promotes it on success, which
            #: makes completion a claim by the one component that observes it.
            "produced_status": "PLAN_DRAFTING",
            #: How the produced document names itself. A template rather than
            #: a hardcoded noun because `produces` is configurable: a role
            #: that produces something other than a plan must not be handed
            #: prose about plans. Without one, the source's own title carries
            #: through — mediocre, never wrong.
            "produced_title": "{title} implementation plan",
            "prompt_template": (
                "Read the spec at {file} and create a detailed TDD implementation plan "
                "using the writing-plans skill. Save it in docs/superpowers/plans/.\n\n"
                "The plan must open with exactly this YAML frontmatter, before its first "
                "heading — the pipeline reads that block, and a plan without it is "
                "invisible to the state machine and cannot pass its next gate:\n\n"
                "{frontmatter}\n\n"
                "Do not create a git branch, and do not instruct the implementer to "
                "create one: the dispatcher owns branches and worktrees, and derives the "
                "branch name itself."
            ),
            "extra_args": []
        },
        "executor": {
            "model": "minimax-m3",
            "harness": "opencode",
            "provider": "opencode-go",
            "allowed_statuses": ["PLAN_APPROVED"],
            "in_progress_status": "EXECUTING",
            "success_status": "EXECUTION_COMPLETE",
            "isolated_worktree": True,
            "prompt_template": "Execute the implementation plan at {file} using the subagent-driven-development skill. Check off tasks in the plan as completed.",
            "extra_args": []
        }
    },
    "worktree": {
        #: Untracked files copied into an isolated role's worktree, by path
        #: relative to the project root. Empty by default: a project that
        #: declares nothing gets exactly the tree git builds, and no extra
        #: git call is ever made.
        "copy": [],
    },
    "sandbox": {
        "enabled": False,
        "compose_file": "docker-compose.yml",
        "health_service": None,
        "health_timeout": 60,
        "env": {},
        "teardown": {
            "on_verified_closed": "volumes",
            "on_failed": "containers",
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`.

    Mappings merge key by key so that a partial override inherits the rest of
    the defaults. Scalars and lists are replaced wholesale — a user who lists
    `allowed_statuses` means exactly that list, not an addition to ours.

    `null` removes the key, the meaning RFC 7386 JSON Merge Patch already
    gives it. Without it a project cannot subtract: an `agents:` block naming
    only `executor` still resolves a full `planner` from the defaults, at a
    model the project may have decided against, and the role stays
    dispatchable with nothing on disk saying otherwise. `{}` remains the way
    to say "this key, all defaults", so a partial override is untouched.

    This is behaviour-preserving everywhere else. Every reader of a top-level
    section spells it `config.get(X) or {}`, so *absent* and *present but
    null* already resolved alike; the one default that is itself `None`,
    `sandbox.health_service`, reads the same by either route.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_agent_config(project_root: Path) -> dict:
    """Load `.superpowers/agents.yaml`, deep-merged over DEFAULT_CONFIG.

    Raises ConfigError if the file exists but cannot be parsed: a config we
    cannot read is not a reason to silently run with different settings.
    """
    config_file = Path(project_root) / ".superpowers" / "agents.yaml"
    if not config_file.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        yaml = YAML(typ="rt")
        parsed = yaml.load(config_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to parse {config_file}: {exc}") from exc

    return deep_merge(DEFAULT_CONFIG, _to_plain_dict(parsed))


def resolve_agent(config: dict, role: str) -> dict:
    """Return a copy of an agent's config with global harness defaults applied."""
    agents = config.get("agents") or {}
    if role not in agents:
        # "Not defined" reads as a typo, and for a name nobody ever shipped it
        # is one. For a role the plugin does ship, absence has exactly one
        # cause — the project removed it — and that is a different message: it
        # is not a mistake to correct but a level someone performs by hand.
        if role in DEFAULT_CONFIG["agents"]:
            raise ConfigError(
                f"Agent role '{role}' ships with the plugin, but this project "
                f"removed it (`{role}: null` under `agents:`) — so that level "
                f"is performed by hand here and there is nothing to dispatch. "
                f"Roles this project defines: {sorted(agents)}"
            )
        raise ConfigError(
            f"Agent role '{role}' is not defined in the configuration. "
            f"Defined roles: {sorted(agents)}"
        )
    agent = copy.deepcopy(agents[role])
    harness = config.get("harness") or {}
    if "harness" not in agent and harness.get("default"):
        agent["harness"] = harness["default"]
    if "provider" not in agent and harness.get("provider"):
        agent["provider"] = harness["provider"]
    return agent


KNOWN_AGENT_KEYS = frozenset({
    "model",
    "harness",
    "provider",
    "allowed_statuses",
    "in_progress_status",
    "success_status",
    "isolated_worktree",
    "prompt_template",
    "extra_args",
    "harness_adapter",
    "skills",
    "instructions",
    "produces",
    "produced_title",
    "produced_status",
})


KNOWN_WORKTREE_KEYS = frozenset({"copy"})


KNOWN_SANDBOX_KEYS = frozenset({
    "enabled", "compose_file", "health_service", "health_timeout",
    "env", "teardown",
})

KNOWN_TEARDOWN_KEYS = frozenset({"on_verified_closed", "on_failed"})

#: `volumes` destroys data and releases the address; `containers` stops the
#: stack but keeps both, so a failure stays diagnosable; `none` leaves it up.
TEARDOWN_MODES = frozenset({"volumes", "containers", "none"})

#: The only substitutions a `sandbox.env` template may contain.
KNOWN_TOKENS = frozenset({"ip", "project"})

_TOKEN_PATTERN = re.compile(r"(?<!\$)\{([^{}]*)\}")


def validate_config(config: dict) -> None:
    """Fail closed on a configuration that cannot work.

    Catching a typo here is the difference between a readable error and an
    agent dispatched with a silently disabled state gate.
    """
    state_machine = config.get("state_machine") or {}
    valid_statuses = state_machine.get("valid_statuses") or []
    if not valid_statuses:
        raise ConfigError("state_machine.valid_statuses is missing or empty.")
    known = set(valid_statuses)

    for source, targets in (state_machine.get("transitions") or {}).items():
        if source not in known:
            raise ConfigError(
                f"state_machine.transitions: unknown source status '{source}'. "
                f"Known statuses: {sorted(known)}"
            )
        for target in targets or []:
            if target not in known:
                raise ConfigError(
                    f"state_machine.transitions['{source}']: unknown target status "
                    f"'{target}'. Known statuses: {sorted(known)}"
                )

    for role, agent in (config.get("agents") or {}).items():
        # Before anything reads a key off it. `set(agent)` over a string
        # iterates characters, and the loop below then reported
        # `unknown key(s) ['-', '3', 'i', 'k', 'm']` for `planner: kimi-k3` —
        # confident, fluent and wrong, which costs the reader more than a
        # crash would.
        if not isinstance(agent, dict):
            raise ConfigError(
                f"agent '{role}' must be a mapping of settings, got "
                f"{type(agent).__name__}. Writing the value where the mapping "
                f"goes is the common slip: `{role}:` on one line, then "
                f"`model: ...` indented under it. To remove the role from this "
                f"project entirely, write `{role}: null`."
            )

        unknown_keys = set(agent) - KNOWN_AGENT_KEYS
        if unknown_keys:
            raise ConfigError(
                f"agent '{role}': unknown key(s) {sorted(unknown_keys)}. "
                f"Known keys: {sorted(KNOWN_AGENT_KEYS)}"
            )
        for key in ("in_progress_status", "success_status"):
            value = agent.get(key)
            if value is not None and value not in known:
                raise ConfigError(
                    f"agent '{role}'.{key} = '{value}' is not in valid_statuses."
                )
        # An entry gate is mandatory. Absent, `null` and `[]` all resolved to
        # the same thing and the dispatch guard read it as "this role has no
        # gate" — dispatchable from any status, `VERIFIED_CLOSED` included,
        # and from a status another role's supervisor currently owns. The
        # three spellings a reader would use for "no document is acceptable"
        # meant "every document is", and nothing said so.
        if not agent.get("allowed_statuses"):
            raise ConfigError(
                f"agent '{role}' declares no allowed_statuses, so nothing says "
                f"which documents it may be dispatched at — and an empty gate "
                f"is not a closed one. List the statuses this role starts from "
                f"(a partial override inherits them; only a role that declares "
                f"the key sets it). To stop dispatching the role at all, remove "
                f"it: `{role}: null`."
            )
        for status in agent["allowed_statuses"]:
            if status not in known:
                raise ConfigError(
                    f"agent '{role}'.allowed_statuses contains unknown status '{status}'."
                )
        skills = agent.get("skills")
        if skills is not None:
            if not isinstance(skills, list):
                raise ConfigError(
                    f"agent '{role}'.skills must be a list of skill names, "
                    f"got {type(skills).__name__}. A bare string is the common "
                    f"slip: write `skills: [name]`, not `skills: name`."
                )
            for entry in skills:
                if not isinstance(entry, str) or not entry.strip():
                    raise ConfigError(
                        f"agent '{role}'.skills contains {entry!r}: every skill "
                        f"name must be a non-empty string."
                    )

        produces = agent.get("produces")
        if produces is not None and not isinstance(produces, str):
            raise ConfigError(
                f"agent '{role}'.produces must be the name of the directory the "
                f"role's document lands in (for example `plans`), got "
                f"{type(produces).__name__}."
            )

        produced_title = agent.get("produced_title")
        if produced_title is not None:
            if not isinstance(produced_title, str):
                raise ConfigError(
                    f"agent '{role}'.produced_title must be a template string "
                    f"such as '{{title}} implementation plan', got "
                    f"{type(produced_title).__name__}."
                )
            # Caught here rather than at dispatch: a bad token would raise
            # KeyError deep inside prompt assembly, after the slice had already
            # been moved to its in-progress status.
            for token in _TOKEN_PATTERN.findall(produced_title):
                if token != "title":
                    raise ConfigError(
                        f"agent '{role}'.produced_title: unknown template token "
                        f"'{{{token}}}'. The only token is {{title}}, the source "
                        f"document's own title."
                    )

        instructions = agent.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ConfigError(
                f"agent '{role}'.instructions must be a single string, got "
                f"{type(instructions).__name__}. A list is the common slip: "
                f"these are prose rules appended to the role's prompt, not "
                f"named items, so write them as one YAML block scalar "
                f"(`instructions: |`)."
            )

    worktree = config.get("worktree") or {}
    unknown_keys = set(worktree) - KNOWN_WORKTREE_KEYS
    if unknown_keys:
        raise ConfigError(
            f"worktree: unknown key(s) {sorted(unknown_keys)}. "
            f"Known keys: {sorted(KNOWN_WORKTREE_KEYS)}"
        )

    entries = worktree.get("copy")
    if entries is not None:
        # Shape only. Whether a path exists, escapes the project or is already
        # tracked needs a project root, which this function does not have and
        # should not acquire: those are dispatch-time questions and they are
        # asked at the dispatch gate, where the answer can name the worktree.
        if not isinstance(entries, list):
            raise ConfigError(
                f"worktree.copy must be a list of paths relative to the "
                f"project root, got {type(entries).__name__}. A bare string is "
                f"the common slip: write `copy: [.env]`, not `copy: .env`."
            )
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigError(
                    f"worktree.copy contains {entry!r}: every entry must be a "
                    f"non-empty path relative to the project root."
                )

    sandbox = config.get("sandbox") or {}
    unknown_keys = set(sandbox) - KNOWN_SANDBOX_KEYS
    if unknown_keys:
        raise ConfigError(
            f"sandbox: unknown key(s) {sorted(unknown_keys)}. "
            f"Known keys: {sorted(KNOWN_SANDBOX_KEYS)}"
        )

    teardown = sandbox.get("teardown") or {}
    unknown_teardown = set(teardown) - KNOWN_TEARDOWN_KEYS
    if unknown_teardown:
        raise ConfigError(
            f"sandbox.teardown: unknown key(s) {sorted(unknown_teardown)}. "
            f"Known keys: {sorted(KNOWN_TEARDOWN_KEYS)}"
        )
    for key, mode in teardown.items():
        if mode not in TEARDOWN_MODES:
            raise ConfigError(
                f"sandbox.teardown.{key} = '{mode}' is not a teardown mode. "
                f"Valid modes: {sorted(TEARDOWN_MODES)}"
            )

    for name, template in (sandbox.get("env") or {}).items():
        for token in _TOKEN_PATTERN.findall(str(template)):
            if token not in KNOWN_TOKENS:
                raise ConfigError(
                    f"sandbox.env.{name}: unknown template token '{{{token}}}'. "
                    f"Known tokens: {sorted('{' + t + '}' for t in KNOWN_TOKENS)}"
                )
