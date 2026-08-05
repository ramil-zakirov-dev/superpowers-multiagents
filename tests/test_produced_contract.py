"""What the dispatcher promises a produced document will contain.

The prompt says the block must be reproduced *exactly*, so whatever is
missing from it is missing from every generated document — this is not a
default a good agent improves on, it is an instruction a good agent obeys.

Four keys were missing against the convention every hand-written document in
this repository follows. Three of them cost readability. The fourth,
`depends_on`, costs a gate: `check_unmet_dependencies` reads the *dispatched*
document, and the executor is dispatched at the plan. A plan that dropped the
spec's dependencies did not look poorer — it silently stopped being held back
by them.
"""

import pytest

from scripts.orchestrator import cmd_dispatch_agent
from tests.test_dispatch_integration import RECORDING_ADAPTER, _args
from tests.conftest import REPO_ROOT

RICH_SPEC = """---
slice_id: "slice-01-demo"
title: "Checkout flow"
status: SPEC_APPROVED
target_version: "2.4.0"
depends_on: ["billing-api"]
---

# Checkout flow
"""


def _record_prompt(project_root, monkeypatch, tmp_path):
    log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(log))
    (project_root / "recording_adapter.py").write_text(
        RECORDING_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    harness_adapter: 'recording_adapter.py'\n"
        "    isolated_worktree: false\n",
        encoding="utf-8",
    )
    return log


@pytest.fixture
def rich_dispatch(tmp_project, demo_spec, monkeypatch, tmp_path):
    """A planner dispatched at a spec written to this repo's own convention.

    The dependency it declares is *closed* here — the gate runs before the
    prompt is assembled, and this fixture is about what the prompt says, not
    about whether the gate works. The gate's own behaviour is asserted below.
    """
    demo_spec.write_text(RICH_SPEC, encoding="utf-8")
    (demo_spec.parent / "billing-api-design.md").write_text(
        '---\nslice_id: "billing-api"\nstatus: VERIFIED_CLOSED\n---\n\n# Billing\n',
        encoding="utf-8",
    )
    log = _record_prompt(tmp_project, monkeypatch, tmp_path)
    cmd_dispatch_agent(_args(demo_spec))
    return log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    [
        'slice_id: "slice-01-demo"',
        'title: "Checkout flow implementation plan"',
        "status: PLAN_GENERATED",
        'target_version: "2.4.0"',
        'depends_on: ["billing-api"]',
    ],
)
def test_the_prompt_states_the_whole_contract(rich_dispatch, line):
    assert line in rich_dispatch


def test_the_prompt_points_the_plan_back_at_its_spec(rich_dispatch):
    """Rendered by the dispatcher, which knows the path, rather than asked of
    the agent, which would have to guess it.
    """
    assert 'spec: "docs/superpowers/specs/' in rich_dispatch


def test_the_prompt_does_not_ask_the_plan_to_claim_the_specs_lenses(rich_dispatch):
    """`lenses:` records which ways of thinking a document was reasoned
    through. Copying the spec's list into the plan's mandated block would have
    the plan assert a use nothing observed — the same shape of unearned claim
    the rest of this pipeline spent two slices removing.
    """
    assert "lenses:" not in rich_dispatch


# --- the consequence, not the cosmetics ---


def _plan_with(tmp_project, depends_on: str):
    plans = tmp_project / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / "2026-08-05-slice-01-demo-plan.md"
    plan.write_text(
        f'---\nslice_id: "slice-01-demo"\nstatus: PLAN_APPROVED\n{depends_on}---\n\n# Plan\n',
        encoding="utf-8",
    )
    return plan


def _unclosed_dependency(tmp_project):
    specs = tmp_project / "docs" / "superpowers" / "specs"
    (specs / "billing-api-design.md").write_text(
        '---\nslice_id: "billing-api"\nstatus: EXECUTING\n---\n\n# Billing\n',
        encoding="utf-8",
    )


def test_a_plan_that_kept_its_dependency_holds_the_executor_back(tmp_project, capsys):
    """What the missing key was costing. The gate reads the plan, so this is
    the only place the spec's `depends_on` can still do its job.
    """
    _unclosed_dependency(tmp_project)
    plan = _plan_with(tmp_project, 'depends_on: ["billing-api"]\n')

    with pytest.raises(SystemExit) as excinfo:
        cmd_dispatch_agent(_args(plan, role="executor"))

    out = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert "Dependency Gate" in out
    assert "billing-api" in out


def test_a_plan_that_dropped_it_sails_straight_through(tmp_project, capsys, monkeypatch):
    """The defect stated as a passing test, so it stays visible: the same
    unmet dependency, the same slice, and nothing stops the dispatch — because
    the document the gate reads no longer mentions it.
    """
    _unclosed_dependency(tmp_project)
    plan = _plan_with(tmp_project, "")

    from scripts.dependencies import check_unmet_dependencies

    assert check_unmet_dependencies(plan) == [], (
        "a plan with no depends_on cannot be held back by the spec's"
    )
