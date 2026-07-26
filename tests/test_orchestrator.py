import pytest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import patch
import json

from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.hooks import load_project_hooks, run_infrastructure_hook
from scripts.dependencies import check_unmet_dependencies
from scripts.utils import find_project_root, _sanitize_id, _to_plain_dict
from scripts.locks import acquire_slice_lock, release_slice_lock
from scripts.git_ops import check_working_tree_clean
from scripts.config import DEFAULT_CONFIG


# State machine defaults for tests
_SM = DEFAULT_CONFIG["state_machine"]
_VALID = _SM["valid_statuses"]
_TRANS = _SM["transitions"]


# ===== parse_frontmatter =====

def test_parse_frontmatter():
    sample_md = """---
milestone_id: 1
slice_id: "slice-01"
status: SPEC_APPROVED
depends_on:
  - "slice-00-base"
---

# Feature Title

Some content here.
"""
    data = parse_frontmatter(sample_md)
    assert data["milestone_id"] == 1
    assert data["slice_id"] == "slice-01"
    assert data["status"] == "SPEC_APPROVED"
    assert data["depends_on"] == ["slice-00-base"]


def test_parse_frontmatter_no_frontmatter():
    """Files without frontmatter should return empty dict."""
    data = parse_frontmatter("# Just a heading\n\nSome text.")
    assert data == {}


def test_parse_frontmatter_malformed_yaml():
    """Malformed YAML should return empty dict, not crash."""
    malformed = """---
status: [this is broken
  - unclosed bracket
title: "also: broken: yaml
---

# Content
"""
    data = parse_frontmatter(malformed)
    assert data == {}


def test_parse_frontmatter_multiline_yaml():
    """Multiline YAML values should be preserved correctly by ruamel.yaml."""
    multiline_md = """---
title: "Feature X"
description: |
  This is a long description
  that spans multiple lines
  and should be preserved.
status: DRAFT_SPEC
tags:
  - auth
  - security
---

# Content
"""
    data = parse_frontmatter(multiline_md)
    assert data["title"] == "Feature X"
    assert "multiple lines" in data["description"]
    assert data["status"] == "DRAFT_SPEC"
    assert data["tags"] == ["auth", "security"]


def test_parse_frontmatter_returns_plain_dict():
    """parse_frontmatter should return plain Python types, not ruamel CommentedMap."""
    sample_md = """---
nested:
  key: value
list:
  - item1
  - item2
---
"""
    data = parse_frontmatter(sample_md)
    assert type(data) is dict
    assert type(data["nested"]) is dict
    assert type(data["list"]) is list


# ===== update_frontmatter_status =====

def test_update_frontmatter_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec-design.md"
        spec_file.write_text("""---
title: "Test Feature"
status: DRAFT_SPEC
---

# Content
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "SPEC_APPROVED", _VALID, _TRANS) is True
        
        updated_data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert updated_data["status"] == "SPEC_APPROVED"


def test_update_invalid_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec-design.md"
        spec_file.write_text("""---
status: DRAFT_SPEC
---
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "INVALID_STATE", _VALID, _TRANS) is False


def test_update_status_forbidden_transition():
    """Jumping from DRAFT_SPEC directly to VERIFIED_CLOSED should be rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec.md"
        spec_file.write_text("""---
status: DRAFT_SPEC
---
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "VERIFIED_CLOSED", _VALID, _TRANS) is False
        # Verify status unchanged
        data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert data["status"] == "DRAFT_SPEC"


def test_update_status_forbidden_skip_planning():
    """Jumping from SPEC_APPROVED to EXECUTION_COMPLETE should be rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec.md"
        spec_file.write_text("""---
status: SPEC_APPROVED
---
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "EXECUTION_COMPLETE", _VALID, _TRANS) is False


def test_update_status_merge_conflict_reachable_from_executing():
    """EXECUTING -> MERGE_CONFLICT should be a valid transition."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec.md"
        spec_file.write_text("""---
status: EXECUTING
---
""", encoding="utf-8")

        assert update_frontmatter_status(spec_file, "MERGE_CONFLICT", _VALID, _TRANS) is True
        data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert data["status"] == "MERGE_CONFLICT"


def test_update_status_file_not_found():
    """Updating a non-existent file should return False."""
    result = update_frontmatter_status(Path("/nonexistent/file.md"), "DRAFT_SPEC", _VALID, _TRANS)
    assert result is False


def test_update_status_preserves_other_fields():
    """Updating status should not corrupt other frontmatter fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec.md"
        spec_file.write_text("""---
title: "Important Feature"
slice_id: "slice-42"
status: DRAFT_SPEC
depends_on:
  - "slice-01"
  - "slice-02"
---

# Body content that must survive
""", encoding="utf-8")

        update_frontmatter_status(spec_file, "SPEC_APPROVED", _VALID, _TRANS)
        data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))

        assert data["title"] == "Important Feature"
        assert data["slice_id"] == "slice-42"
        assert data["status"] == "SPEC_APPROVED"
        assert data["depends_on"] == ["slice-01", "slice-02"]
        assert "Body content that must survive" in spec_file.read_text(encoding="utf-8")


# ===== State Transition Coverage =====

def test_full_happy_path_transitions():
    """Walk through the entire state machine from DRAFT_SPEC to VERIFIED_CLOSED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "test-spec.md"
        spec_file.write_text("""---
status: DRAFT_SPEC
---
""", encoding="utf-8")

        transitions = [
            "SPEC_APPROVED", "PLANNING", "PLAN_GENERATED",
            "PLAN_APPROVED", "EXECUTING", "EXECUTION_COMPLETE",
            "VERIFIED_CLOSED"
        ]

        for target_status in transitions:
            assert update_frontmatter_status(spec_file, target_status, _VALID, _TRANS) is True, \
                f"Transition to {target_status} should succeed"

        data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert data["status"] == "VERIFIED_CLOSED"


# ===== load_project_hooks =====

def test_load_project_hooks():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_slice_execution_start:
    command: "echo test"
""", encoding="utf-8")

        hooks = load_project_hooks(project_root)
        assert "on_slice_execution_start" in hooks
        assert hooks["on_slice_execution_start"]["command"] == "echo test"


def test_load_project_hooks_returns_plain_dict():
    """load_project_hooks should return plain dict, not CommentedMap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_test:
    command: "echo hello"
    capture_env: true
""", encoding="utf-8")

        hooks = load_project_hooks(project_root)
        assert type(hooks) is dict
        assert type(hooks["on_test"]) is dict


def test_load_project_hooks_missing_file():
    """Missing hooks.yaml should return empty dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        hooks = load_project_hooks(Path(tmpdir))
        assert hooks == {}


# ===== run_infrastructure_hook =====

def test_run_infrastructure_hook():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_slice_execution_start:
    command: "echo LOOPBACK_IP=127.0.0.9"
    capture_env: true
""", encoding="utf-8")

        env = run_infrastructure_hook("on_slice_execution_start", project_root=project_root)
        assert env.get("LOOPBACK_IP") == "127.0.0.9"


def test_run_infrastructure_hook_missing_event():
    """Running a hook for an undefined event should just return the current env."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        (sp_dir / "hooks.yaml").write_text("hooks:\n  on_test:\n    command: echo hi\n", encoding="utf-8")

        env = run_infrastructure_hook("on_nonexistent_event", project_root=project_root)
        assert isinstance(env, dict)


# ===== check_unmet_dependencies =====

def test_check_unmet_dependencies():
    with tempfile.TemporaryDirectory() as tmpdir:
        specs_dir = Path(tmpdir) / "specs"
        specs_dir.mkdir()

        dep_spec = specs_dir / "2026-07-25-slice-01-base-design.md"
        dep_spec.write_text("""---
slice_id: "slice-01-base"
status: EXECUTING
---
""", encoding="utf-8")

        target_spec = specs_dir / "2026-07-25-slice-02-dep-design.md"
        target_spec.write_text("""---
slice_id: "slice-02-dep"
status: SPEC_APPROVED
depends_on:
  - "slice-01-base"
---
""", encoding="utf-8")

        unmet = check_unmet_dependencies(target_spec)
        assert len(unmet) == 1
        assert "slice-01-base" in unmet[0]

        # Update base to VERIFIED_CLOSED through valid transitions
        update_frontmatter_status(dep_spec, "EXECUTION_COMPLETE", _VALID, _TRANS)
        update_frontmatter_status(dep_spec, "VERIFIED_CLOSED", _VALID, _TRANS)
        unmet_after = check_unmet_dependencies(target_spec)
        assert len(unmet_after) == 0


def test_check_unmet_dependencies_no_deps():
    """A spec with no depends_on should return an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        specs_dir = Path(tmpdir) / "specs"
        specs_dir.mkdir()
        spec = specs_dir / "standalone.md"
        spec.write_text("""---
status: SPEC_APPROVED
---
""", encoding="utf-8")
        assert check_unmet_dependencies(spec) == []


# ===== find_project_root =====

def test_find_project_root_with_git():
    """Should find project root by .git directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".git").mkdir()
        deep_path = root / "docs" / "superpowers" / "specs"
        deep_path.mkdir(parents=True)
        spec_file = deep_path / "test.md"
        spec_file.write_text("test", encoding="utf-8")

        found = find_project_root(spec_file)
        assert found == root.resolve()


def test_find_project_root_with_superpowers():
    """Should prefer .superpowers marker over .git."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".git").mkdir()
        (root / ".superpowers").mkdir()
        deep_path = root / "a" / "b" / "c"
        deep_path.mkdir(parents=True)

        found = find_project_root(deep_path)
        assert found == root.resolve()


# ===== _sanitize_id =====

def test_sanitize_id_valid():
    """Valid IDs should pass through."""
    assert _sanitize_id("slice-01-auth") == "slice-01-auth"
    assert _sanitize_id("feature_v2.0") == "feature_v2.0"


def test_sanitize_id_rejects_shell_metacharacters():
    """IDs with shell metacharacters must raise, not exit the process."""
    from scripts.errors import ValidationError

    for bad in ("slice; rm -rf /", "slice && curl evil.com", "slice | cat /etc/passwd"):
        with pytest.raises(ValidationError):
            _sanitize_id(bad)


# ===== _to_plain_dict =====

def test_to_plain_dict_with_primitives():
    """Primitives should pass through unchanged."""
    assert _to_plain_dict("hello") == "hello"
    assert _to_plain_dict(42) == 42
    assert _to_plain_dict(True) is True
    assert _to_plain_dict(None) is None


# ===== create_git_worktree =====

def test_create_git_worktree_rejects_malicious_id():
    """Worktree creation with a shell-injection attempt must raise ValidationError."""
    from scripts.errors import ValidationError
    from scripts.git_ops import create_git_worktree

    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValidationError):
            create_git_worktree("foo; rm -rf /", project_root=Path(tmpdir))


def test_custom_adapter_import_leaves_no_pycache():
    """A custom adapter must not dirty the user's tree with __pycache__."""
    from scripts.adapters.loader import get_harness_adapter

    with tempfile.TemporaryDirectory() as tmp_path_str:
        tmp_path = Path(tmp_path_str)
        (tmp_path / "my_adapter.py").write_text(
            "from scripts.adapters.base import HarnessAdapter\n"
            "class Mine(HarnessAdapter):\n"
            "    def build_command(self, agent_config, task_prompt):\n"
            "        return ['echo', task_prompt]\n",
            encoding="utf-8",
        )
        get_harness_adapter({"harness_adapter": "my_adapter.py"}, tmp_path)
        assert not (tmp_path / "__pycache__").exists()


# ===== cmd_summary =====

def test_cmd_summary(capsys):
    """cmd_summary should print the last 50 lines of the matching log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logs_dir = Path(tmpdir) / "logs"
        logs_dir.mkdir()
        log_file = logs_dir / "executor_slice-01-auth.log"
        log_content = "\n".join([f"Line {i}: test output" for i in range(100)])
        log_file.write_text(log_content, encoding="utf-8")

        # Patch Path("logs") to point to our temp dir
        from scripts.orchestrator import cmd_summary
        import argparse
        args = argparse.Namespace(slice="slice-01-auth")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmpdir)
            cmd_summary(args)
        finally:
            os.chdir(original_cwd)

        captured = capsys.readouterr()
        assert "slice-01-auth" in captured.out
        assert "Line 99: test output" in captured.out
        # Should only show last 50 lines
        assert "Line 49: test output" not in captured.out
        assert "Line 50: test output" in captured.out


# ===== cmd_trigger_hook =====

def test_cmd_trigger_hook():
    """trigger-hook command should execute the named hook."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        sp_dir = project_root / ".superpowers"
        sp_dir.mkdir()
        hooks_file = sp_dir / "hooks.yaml"
        hooks_file.write_text("""hooks:
  on_test_event:
    command: "echo HOOK_FIRED=true"
    capture_env: true
""", encoding="utf-8")

        from scripts.orchestrator import cmd_trigger_hook
        import argparse
        args = argparse.Namespace(event="on_test_event", dir=str(project_root))
        # Should not raise
        cmd_trigger_hook(args)


# ===== BOM tolerance =====

def test_parse_frontmatter_with_bom():
    """Files starting with UTF-8 BOM should still parse correctly."""
    bom_content = "\ufeff---\nstatus: DRAFT_SPEC\ntitle: BOM Test\n---\n\n# Content\n"
    data = parse_frontmatter(bom_content)
    assert data["status"] == "DRAFT_SPEC"
    assert data["title"] == "BOM Test"


def test_update_frontmatter_with_bom():
    """update_frontmatter_status should work on BOM files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_file = Path(tmpdir) / "bom-test.md"
        spec_file.write_text("\ufeff---\nstatus: DRAFT_SPEC\n---\n\n# Content\n", encoding="utf-8-sig")

        assert update_frontmatter_status(spec_file, "SPEC_APPROVED", _VALID, _TRANS) is True
        data = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
        assert data["status"] == "SPEC_APPROVED"


# ===== Slice Locking =====



# ===== Model defaults =====

def test_default_model_constants():
    """Default model constants should be defined in DEFAULT_CONFIG."""
    assert DEFAULT_CONFIG["agents"]["planner"]["model"] == "kimi-k3"
    assert DEFAULT_CONFIG["agents"]["executor"]["model"] == "minimax-m3"


# ===== Adapter =====

def test_opencode_adapter_returns_argv_list():
    """shell=False dispatch requires an argv list, not a shell string."""
    from scripts.adapters.opencode import OpenCodeAdapter

    adapter = OpenCodeAdapter()
    config = {"model": "kimi-k3", "provider": "opencode-go", "extra_args": []}
    argv = adapter.build_command(config, "Do something")
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert argv == ["opencode", "run", "--model", "opencode-go/kimi-k3", "Do something"]


def test_opencode_adapter_passes_provider_with_model():
    """The provider was silently dropped under the default empty extra_args."""
    from scripts.adapters.opencode import OpenCodeAdapter

    argv = OpenCodeAdapter().build_command(
        {"model": "minimax-m3", "provider": "opencode-go", "extra_args": []}, "Task"
    )
    assert "opencode-go/minimax-m3" in argv


def test_opencode_adapter_with_extra_args():
    from scripts.adapters.opencode import OpenCodeAdapter

    argv = OpenCodeAdapter().build_command(
        {"model": "kimi-k3", "provider": "opencode-go", "extra_args": ["--provider={provider}"]},
        "Test prompt",
    )
    assert "--provider=opencode-go" in argv


def test_prompt_is_a_single_argv_element_not_shell_quoted():
    """A prompt containing quotes must survive verbatim — no shell involved."""
    from scripts.adapters.opencode import OpenCodeAdapter

    prompt = """Read C:\\path\\file.md and say "hello" — don't quote it"""
    argv = OpenCodeAdapter().build_command({"model": "m", "provider": "p"}, prompt)
    assert argv[-1] == prompt
