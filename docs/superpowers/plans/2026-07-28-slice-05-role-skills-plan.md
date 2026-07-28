---
slice_id: "slice-05-role-skills"
title: "Role skills implementation plan"
status: PLAN_GENERATED
target_version: "2.4.0"
spec: "docs/superpowers/specs/2026-07-28-slice-05-role-skills-design.md"
depends_on: []
---

# Role Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a project name skills for a dispatched agent role in `agents.yaml`, appended to the default prompt instead of replacing it, with a warning when the harness cannot see a named skill.

**Architecture:** A new pure module `scripts/skills.py` owns normalisation, prompt composition, and the visible/invisible comparison. A new `HarnessAdapter.list_skills()` seam owns the harness-specific question of what exists, defaulting to `None` ("cannot tell"). `orchestrator.py` wires the two together and prints a hint beside the existing one.

**Tech Stack:** Python 3.11, `ruamel.yaml`, `pytest`. No new dependencies.

## Global Constraints

- No test may invoke a real harness, a real container runtime, or the network. The `opencode` binary is installed on the development machine and a live run costs money.
- `None` from `list_skills()` is not an empty set. `None` = cannot tell, stay silent. Empty set = harness reports nothing, every named skill is missing.
- A malformed `skills` value fails closed (`ConfigError`). A skill the harness cannot see only warns; the dispatch proceeds.
- The plugin ships **no** default skill list for any role. `deep_merge` replaces lists wholesale, so a default would be silently destroyed by any project that adds one lens of its own.
- Absent `skills` must change nothing: no added prompt text, no subprocess, no output.
- Conventional Commits; end each commit message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Run the suite as `python -m pytest -q -p no:cacheprovider`. Baseline on this branch is **326 passed**.

## File Structure

| File | Responsibility |
|---|---|
| `scripts/skills.py` (create) | Pure functions: `declared_skills`, `compose_prompt`, `invisible_skills`. No I/O, no subprocess. |
| `scripts/config.py` (modify) | `skills` in `KNOWN_AGENT_KEYS`; shape validation in `validate_config`. |
| `scripts/adapters/base.py` (modify) | `list_skills()` returning `None`. |
| `scripts/adapters/opencode.py` (modify) | `list_skills()` via `opencode debug skill`. |
| `scripts/orchestrator.py` (modify) | Compose the prompt; `_warn_if_invisible_skills` beside `_warn_if_artifacts_not_ignored`. |
| `tests/test_skills.py` (create) | Unit tests for the pure module and both adapters. |
| `tests/test_config.py` (modify) | Schema acceptance and rejection. |
| `tests/test_dispatch_integration.py` (modify) | One end-to-end case: the sentence reaches the agent's argv. |
| `docs/configuration.md`, `README.md`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (modify) | Documentation and version. |

---

### Task 1: Schema accepts and validates `skills`

**Files:**
- Modify: `scripts/config.py:135-146` (`KNOWN_AGENT_KEYS`), `scripts/config.py:191-208` (per-role loop in `validate_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a config in which `agent["skills"]` is either absent or a list of non-empty strings. Every later task may assume that shape without re-checking.

- [ ] **Step 1: Write the failing tests**

```python
def test_skills_key_is_accepted():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["agents"]["planner"]["skills"] = ["clean-architecture"]
    validate_config(config)          # must not raise


@pytest.mark.parametrize("value", [
    "clean-architecture",            # a bare string is the likely typo
    ["clean-architecture", 7],
    ["clean-architecture", ""],
    ["clean-architecture", "   "],
])
def test_malformed_skills_is_refused(value):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["agents"]["planner"]["skills"] = value
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert "planner" in str(excinfo.value)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_config.py -k skills -q -p no:cacheprovider`
Expected: `test_skills_key_is_accepted` FAILS with `ConfigError: agent 'planner': unknown key(s) ['skills']`; the parametrised cases FAIL because no `ConfigError` is raised for the list variants.

- [ ] **Step 3: Add the key to the known set**

In `scripts/config.py`, add `"skills"` to `KNOWN_AGENT_KEYS`, keeping the existing alphabetical-ish grouping:

```python
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
})
```

- [ ] **Step 4: Validate the shape**

In `validate_config`, inside the existing `for role, agent in (config.get("agents") or {}).items():` loop, after the `allowed_statuses` check:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config.py -q -p no:cacheprovider`
Expected: PASS, no regressions in the rest of the file.

- [ ] **Step 6: Commit**

```bash
git add scripts/config.py tests/test_config.py
git commit -m "feat(config): accept and validate a per-role skills list"
```

---

### Task 2: The pure skills module

**Files:**
- Create: `scripts/skills.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: an agent config dict validated by Task 1.
- Produces:
  - `declared_skills(agent_config: dict) -> list[str]`
  - `compose_prompt(task_prompt: str, skills: list[str]) -> str`
  - `invisible_skills(skills: list[str], visible: set[str] | None) -> list[str]`

  Task 4 calls all three; Task 3 supplies the `visible` argument.

- [ ] **Step 1: Write the failing tests**

```python
from scripts.skills import compose_prompt, declared_skills, invisible_skills


def test_declared_skills_absent_is_empty():
    assert declared_skills({"model": "kimi-k3"}) == []


def test_declared_skills_preserves_order_and_dedupes():
    config = {"skills": ["clean-architecture", "clean-code", "clean-architecture"]}
    assert declared_skills(config) == ["clean-architecture", "clean-code"]


def test_declared_skills_strips_whitespace():
    assert declared_skills({"skills": [" clean-code "]}) == ["clean-code"]


def test_compose_prompt_without_skills_is_unchanged():
    assert compose_prompt("Read the spec at /tmp/s.md", []) == "Read the spec at /tmp/s.md"


def test_compose_prompt_appends_one_paragraph():
    composed = compose_prompt("Read the spec.", ["clean-architecture", "clean-code"])
    assert composed == (
        "Read the spec.\n\n"
        "Use these skills where they apply: clean-architecture, clean-code."
    )


def test_invisible_skills_reports_only_the_missing_ones():
    assert invisible_skills(["a", "b"], {"a"}) == ["b"]


def test_invisible_skills_is_silent_when_the_adapter_cannot_tell():
    assert invisible_skills(["a", "b"], None) == []


def test_invisible_skills_reports_all_when_the_harness_sees_none():
    assert invisible_skills(["a", "b"], set()) == ["a", "b"]
```

The last two are the pair that matters. They are the executable statement that
`None` and `set()` are different answers.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_skills.py -q -p no:cacheprovider`
Expected: FAIL, `ModuleNotFoundError: No module named 'scripts.skills'`.

- [ ] **Step 3: Write the module**

```python
"""Optional per-role skill reinforcement.

A role may name skills it wants a dispatched agent to use. The names are
appended to the rendered prompt rather than substituted into it: `deep_merge`
replaces scalars, so a project forced to edit `prompt_template` in order to
mention a skill would own a fork of the plugin's default prompt forever.

Everything here is pure. Whether a skill exists is a question for the harness
adapter, and printing is the orchestrator's job.
"""

SKILL_SENTENCE = "Use these skills where they apply: {names}."


def declared_skills(agent_config: dict) -> list[str]:
    """The role's skill names, de-duplicated, first occurrence winning.

    A repeat is a slip with an unambiguous intent, so it is normalised rather
    than refused — our fail-closed rule is about ambiguity, not untidiness.
    """
    ordered: list[str] = []
    for name in agent_config.get("skills") or []:
        cleaned = name.strip()
        if cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def compose_prompt(task_prompt: str, skills: list[str]) -> str:
    """Append the skill sentence to a rendered prompt.

    No skills means the prompt is returned untouched: a project that never
    configured this must not pay a single character for it.
    """
    if not skills:
        return task_prompt
    return f"{task_prompt}\n\n" + SKILL_SENTENCE.format(names=", ".join(skills))


def invisible_skills(skills: list[str], visible: set[str] | None) -> list[str]:
    """Named skills the harness does not report.

    `visible is None` means the adapter cannot answer, which is not the same as
    answering "none". Returning an empty list keeps the caller quiet instead of
    warning about every correctly-named skill on every custom adapter.
    """
    if visible is None:
        return []
    return [name for name in skills if name not in visible]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_skills.py -q -p no:cacheprovider`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/skills.py tests/test_skills.py
git commit -m "feat(skills): pure module for skill normalisation and prompt composition"
```

---

### Task 3: The `list_skills` adapter seam

**Files:**
- Modify: `scripts/adapters/base.py`, `scripts/adapters/opencode.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HarnessAdapter.list_skills(agent_config: dict, cwd: Path) -> set[str] | None` on every adapter. Task 4 calls it with the dispatch's working directory.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skills.py`:

```python
import json
import subprocess
from pathlib import Path

from scripts.adapters.base import HarnessAdapter
from scripts.adapters.opencode import OpenCodeAdapter

DEBUG_SKILL_PAYLOAD = json.dumps([
    {"name": "clean-architecture", "description": "…", "location": "/p/.claude/skills/clean-architecture/SKILL.md"},
    {"name": "customize-opencode", "description": "…", "location": "<built-in>"},
])


def test_base_adapter_cannot_tell():
    assert HarnessAdapter().list_skills({}, Path(".")) is None


def _fake_run(stdout="", returncode=0, raises=None):
    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")
    return run


def test_opencode_adapter_parses_names(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=DEBUG_SKILL_PAYLOAD))
    assert OpenCodeAdapter().list_skills({}, Path(".")) == {
        "clean-architecture", "customize-opencode",
    }


def test_opencode_adapter_runs_in_the_given_cwd(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    OpenCodeAdapter().list_skills({}, Path("/work/tree"))
    assert seen["argv"][:3] == ["opencode", "debug", "skill"]
    assert str(seen["cwd"]) == str(Path("/work/tree"))


@pytest.mark.parametrize("kwargs", [
    {"returncode": 1, "stdout": ""},
    {"stdout": "not json at all"},
    {"stdout": json.dumps({"skills": []})},        # a dict, not the expected list
    {"raises": FileNotFoundError("opencode")},
    {"raises": subprocess.TimeoutExpired("opencode", 60)},
])
def test_opencode_adapter_returns_none_on_any_failure(monkeypatch, kwargs):
    monkeypatch.setattr(subprocess, "run", _fake_run(**kwargs))
    assert OpenCodeAdapter().list_skills({}, Path(".")) is None
```

`test_opencode_adapter_runs_in_the_given_cwd` is not decoration: project-level
skills resolve relative to the working directory, and for an isolated role that
is the worktree, not the project root. Passing the wrong `cwd` would produce
warnings about skills that are in fact present.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_skills.py -k "adapter" -q -p no:cacheprovider`
Expected: FAIL, `AttributeError: 'HarnessAdapter' object has no attribute 'list_skills'`.

- [ ] **Step 3: Add the base method**

In `scripts/adapters/base.py`, inside `HarnessAdapter`, after `build_command`:

```python
    def list_skills(self, agent_config: dict, cwd) -> set[str] | None:
        """Skill names this harness can see from `cwd`, or None if unknowable.

        `None` is not an empty set. An empty set means the harness reports no
        skills at all, so every configured name is missing; `None` means this
        adapter cannot answer and the caller must stay silent. Conflating them
        would make every adapter without an implementation warn about every
        correctly-named skill.
        """
        return None
```

- [ ] **Step 4: Implement it for OpenCode**

In `scripts/adapters/opencode.py`, add the imports and the method:

```python
import json
import subprocess
```

```python
    def list_skills(self, agent_config: dict, cwd) -> set[str] | None:
        """Ask the CLI what it can see. Any failure means "cannot tell".

        `opencode debug skill` prints a JSON array of objects carrying at least
        a `name`. It makes no model call, so this costs nothing but a few
        seconds, and it is only ever reached when a role declares skills.
        """
        try:
            result = subprocess.run(
                ["opencode", "debug", "skill"],
                cwd=str(cwd), capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(payload, list):
            return None

        return {
            entry["name"] for entry in payload
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
```

A diagnostic that can break a dispatch is worse than no diagnostic, which is
why every failure path returns `None` instead of raising.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_skills.py -q -p no:cacheprovider`
Expected: PASS. The file now holds Task 2's 8 tests plus this task's 8 (three
named cases and a five-way parametrisation) = **16**.

- [ ] **Step 6: Commit**

```bash
git add scripts/adapters/base.py scripts/adapters/opencode.py tests/test_skills.py
git commit -m "feat(adapters): add a list_skills seam, implemented for opencode"
```

---

### Task 4: Wire it into dispatch

**Files:**
- Modify: `scripts/orchestrator.py:39-51` (new helper beside `_warn_if_artifacts_not_ignored`), `scripts/orchestrator.py:328-329` (prompt), `scripts/orchestrator.py:415-417` (output)
- Test: `tests/test_dispatch_integration.py`

**Interfaces:**
- Consumes: `scripts.skills.declared_skills`, `compose_prompt`, `invisible_skills` (Task 2); `adapter.list_skills` (Task 3); the validated shape (Task 1).
- Produces: nothing further tasks depend on.

- [ ] **Step 1: Write the failing integration test**

Add to `tests/test_dispatch_integration.py`. It needs an adapter that records
the prompt it was handed, so define one locally rather than changing the shared
fixture:

```python
RECORDING_ADAPTER = '''
import os, sys
sys.path.insert(0, r"{repo_root}")
from scripts.adapters.base import HarnessAdapter


class RecordingAdapter(HarnessAdapter):
    def build_command(self, agent_config, task_prompt):
        with open(os.environ["SUPERPOWERS_PROMPT_LOG"], "w", encoding="utf-8") as handle:
            handle.write(task_prompt)
        return [sys.executable, "-c", "pass"]

    def list_skills(self, agent_config, cwd):
        log = os.environ.get("SUPERPOWERS_SKILLS_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(str(cwd) + "\\n")
        raw = os.environ.get("SUPERPOWERS_VISIBLE_SKILLS")
        if raw is None:
            return None
        return set(filter(None, raw.split(",")))
'''


def _use_recording_adapter(project_root, skills):
    from tests.conftest import REPO_ROOT
    (project_root / "recording_adapter.py").write_text(
        RECORDING_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )
    listed = "\n".join(f"      - {name}" for name in skills)
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    harness_adapter: 'recording_adapter.py'\n"
        "    isolated_worktree: false\n"
        + ("    skills:\n" + listed + "\n" if skills else ""),
        encoding="utf-8",
    )


def test_declared_skills_reach_the_agent_prompt(tmp_project, demo_spec, monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    monkeypatch.setenv("SUPERPOWERS_VISIBLE_SKILLS", "clean-architecture,clean-code")
    _use_recording_adapter(tmp_project, ["clean-architecture", "clean-code"])

    cmd_dispatch_agent(_args(demo_spec))

    assert prompt_log.read_text(encoding="utf-8").endswith(
        "Use these skills where they apply: clean-architecture, clean-code."
    )


def test_no_skills_leaves_the_prompt_untouched(tmp_project, demo_spec, monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    _use_recording_adapter(tmp_project, [])

    cmd_dispatch_agent(_args(demo_spec))

    assert "Use these skills" not in prompt_log.read_text(encoding="utf-8")


def test_invisible_skill_is_reported_and_dispatch_proceeds(
    tmp_project, demo_spec, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("SUPERPOWERS_VISIBLE_SKILLS", "clean-code")
    _use_recording_adapter(tmp_project, ["clean-architcture", "clean-code"])

    cmd_dispatch_agent(_args(demo_spec))

    out = capsys.readouterr().out
    assert "clean-architcture" in out
    assert "not visible to the harness" in out
    assert "Dispatched" in out                      # it still ran
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLANNING"


def test_adapter_that_cannot_tell_stays_silent(
    tmp_project, demo_spec, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.delenv("SUPERPOWERS_VISIBLE_SKILLS", raising=False)
    _use_recording_adapter(tmp_project, ["clean-architecture"])

    cmd_dispatch_agent(_args(demo_spec))

    assert "not visible to the harness" not in capsys.readouterr().out


def test_no_skills_asks_the_harness_nothing(tmp_project, demo_spec, monkeypatch, tmp_path):
    """An unconfigured dispatch must not pay for a subprocess it cannot use."""
    skills_log = tmp_path / "list_skills-calls.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("SUPERPOWERS_SKILLS_LOG", str(skills_log))
    _use_recording_adapter(tmp_project, [])

    cmd_dispatch_agent(_args(demo_spec))

    assert not skills_log.exists(), (
        f"list_skills was called {skills_log.read_text(encoding='utf-8').count(chr(10))} "
        f"time(s) for a role that declares no skills"
    )
```

The adapter writes the call log only when `SUPERPOWERS_SKILLS_LOG` is set, so
the other tests in this group need no change.

The last test guards a stated design property rather than a behaviour a user
sees: absent `skills` must cost nothing. Note what it does *not* do — it does
not monkeypatch `HarnessAdapter.list_skills`, because `RecordingAdapter`
overrides that method and the patch would never be reached. The assertion would
then hold no matter what the orchestrator did. The adapter has to record the
call itself for the test to mean anything.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_dispatch_integration.py -k skill -q -p no:cacheprovider`
Expected: the first FAILS on the `endswith` assertion (the prompt is the bare
template); the third FAILS because nothing is printed about visibility. The
second and fourth pass vacuously — that is expected, and they exist to stay
green when the others turn green.

- [ ] **Step 3: Compose the prompt**

In `scripts/orchestrator.py`, add `from scripts import skills as skills_mod` to
the imports, then replace the two prompt lines:

```python
    prompt_template = agent_config.get("prompt_template", "Process {file}")
    task_prompt = skills_mod.compose_prompt(
        prompt_template.format(file=target_file),
        skills_mod.declared_skills(agent_config),
    )
```

- [ ] **Step 4: Add the warning helper**

Beside `_warn_if_artifacts_not_ignored`, matching its shape — ask, diff, hint,
never fail:

```python
def _warn_if_invisible_skills(agent_config: dict, adapter, cwd: Path) -> None:
    """Say so when a configured skill is not visible to the harness.

    Advisory only: skills are reinforcement, not a dependency, so a name the
    harness cannot resolve must not turn an optional improvement into a new way
    for a dispatch to be blocked.
    """
    declared = skills_mod.declared_skills(agent_config)
    if not declared:
        return
    missing = skills_mod.invisible_skills(declared, adapter.list_skills(agent_config, cwd))
    if missing:
        print(
            "Hint: these skills are not visible to the harness and will have "
            "no effect: " + " ".join(missing)
        )
```

- [ ] **Step 5: Call it**

At the end of `cmd_dispatch_agent`, between the log line and the existing hint:

```python
    print(f"Dispatched {agent_config.get('model')} as {role} (supervisor PID {process.pid}).")
    print(f"Log: {log_file}")
    _warn_if_invisible_skills(agent_config, adapter, cwd)
    _warn_if_artifacts_not_ignored(project_root)
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: baseline 326, plus 5 (Task 1) + 8 (Task 2) + 8 (Task 3) + 5 (this task) = **352 passed**. Confirm the arithmetic against the actual output rather than trusting this number; if it differs, find out why before continuing.

- [ ] **Step 7: Commit**

```bash
git add scripts/orchestrator.py tests/test_dispatch_integration.py
git commit -m "feat(dispatch): append declared skills to the prompt and report invisible ones"
```

---

### Task 5: Documentation and version

**Files:**
- Modify: `docs/configuration.md`, `README.md`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`
- Test: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: the finished behaviour of Tasks 1–4.
- Produces: nothing.

- [ ] **Step 1: Write the failing doc test**

In `tests/test_docs_consistency.py`, beside the existing guards:

```python
def test_every_known_agent_key_is_documented():
    from scripts.config import KNOWN_AGENT_KEYS
    configuration = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    undocumented = sorted(key for key in KNOWN_AGENT_KEYS if f"`{key}`" not in configuration)
    assert not undocumented, (
        "these agent keys exist in the schema but appear nowhere in "
        f"configuration.md: {undocumented}"
    )
```

This guard is bidirectional in the sense that matters: adding a key to the
schema without documenting it now fails.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_docs_consistency.py -k agent_key -q -p no:cacheprovider`
Expected: FAIL, `undocumented: ['skills']`.

- [ ] **Step 3: Document the key**

In `docs/configuration.md`, add `skills` to the agent-key table with the
description "Optional list of skill names appended to the role's prompt", and
add a subsection after it:

> **Skills (per-role reinforcement)**
>
> A role may name skills the dispatched agent should use. The names are appended
> to the rendered `prompt_template` as one sentence, so a project never has to
> copy the plugin's default prompt in order to mention a skill — which would
> fork it permanently, since `deep_merge` replaces scalars.
>
> ```yaml
> agents:
>   planner:
>     skills: [clean-architecture, domain-driven-design]
> ```
>
> The plugin does not install skills and ships no default list. Getting them
> onto disk is the project's business, and a default would be destroyed rather
> than extended by any project that added one of its own, because `deep_merge`
> replaces lists wholesale.
>
> Before dispatch the orchestrator asks the harness adapter which skills it can
> see and prints a hint for any that it cannot. This is advisory: skills are
> reinforcement, not a dependency, and a missing one never blocks a dispatch. A
> malformed `skills` value is a different matter and fails closed.
>
> **For adapter authors:** `list_skills(agent_config, cwd)` returns a set of
> names, or `None` when the harness cannot be asked. Return `None` rather than
> an empty set unless you genuinely know the harness sees nothing — an empty set
> means every configured name is missing and will be reported as such.

- [ ] **Step 4: Update README and version**

Add `skills` to the `agents.yaml` example in `README.md`. Set `"version": "2.4.0"` in `.claude-plugin/plugin.json`. Leave `.claude-plugin/marketplace.json` untouched: its plugin entry carries no `version` field by design — the version comes from `plugin.json` at the source — and adding one now would create a second place to forget.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **353 passed** (352 plus the one doc guard). Verify against the real output.

- [ ] **Step 6: Commit**

```bash
git add docs/configuration.md README.md .claude-plugin tests/test_docs_consistency.py
git commit -m "docs(skills): document the per-role skills key and release 2.4.0"
```

---

## Verification beyond the suite

Green tests prove the wiring, not the value. After Task 5, run these by hand and
record what actually happened:

1. **Mutation — the sentence.** Change `SKILL_SENTENCE` to a different wording.
   Task 4's first test must go red. If it stays green, it is asserting nothing.
2. **Mutation — the `None`/`set()` distinction.** Make `invisible_skills` treat
   `None` as an empty set. `test_invisible_skills_is_silent_when_the_adapter_cannot_tell`
   and `test_adapter_that_cannot_tell_stays_silent` must both go red.
3. **Mutation — the absent-key path.** Make `compose_prompt` append the sentence
   even for an empty list. `test_no_skills_leaves_the_prompt_untouched` must go
   red; if it does not, the vacuous-pass case from Task 4 Step 2 never became
   real.
4. **Mutation — the cwd.** Make `OpenCodeAdapter.list_skills` ignore `cwd`.
   `test_opencode_adapter_runs_in_the_given_cwd` must go red.

Restore the file after each mutation and re-run before starting the next.

**Open risk, carried from the spec §6.** None of this establishes that naming a
skill changes what the executor does. `opencode debug skill` proves visibility,
not influence. Answering that needs one live dispatch and a reading of the diff
it produces, and it is the owner's call whether to spend it.
