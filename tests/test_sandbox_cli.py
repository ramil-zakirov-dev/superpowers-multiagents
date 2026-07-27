import argparse
import json
import sys

import pytest

from scripts.orchestrator import cmd_sandbox


def _args(action, **kwargs):
    base = dict(action=action, dir="", branch="", shell="posix", yes=False, cmd=[])
    base.update(kwargs)
    return argparse.Namespace(**base)


def _enable_sandbox(project_root, env_yaml=""):
    """Write `.superpowers/agents.yaml` with sandbox enabled.

    `cmd_sandbox` loads its config from disk via `load_agent_config`, which is
    independent of whatever config dict a test passes directly to
    `sandbox.ensure_up` to seed state. Without this, the on-disk default
    (`sandbox.enabled: False`) makes `resolve_env` return `{}` regardless of
    the state file `ensure_up` wrote, and every `env` command reports "no
    state" even though a stack was actually brought up. See
    `tests/test_sandbox_dispatch.py`'s `_enable_sandbox` for the same
    pattern used elsewhere in this suite.
    """
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "sandbox:\n"
        "  enabled: true\n"
        "  compose_file: docker-compose.yml\n"
        f"{env_yaml}",
        encoding="utf-8",
    )


def test_env_emits_posix_exports(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {"dsn": "postgres://{ip}:5432/db"}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    _enable_sandbox(
        tmp_project, '  env:\n    dsn: "postgres://{ip}:5432/db"\n'
    )

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha"))

    out = capsys.readouterr().out
    assert "export LOOPBACK_IP=127.0.0." in out
    assert "export dsn=postgres://127.0.0." in out


def test_env_emits_powershell_assignments(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    _enable_sandbox(tmp_project)

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha", shell="powershell"))

    assert '$env:LOOPBACK_IP = "127.0.0.' in capsys.readouterr().out


def test_quote_posix_quotes_trailing_newline():
    """A trailing-newline value must not slip through the unanchored `$`
    loophole in `re.match(r"...$")` (which matches before a trailing `\n`).
    """
    from scripts.orchestrator import _quote_posix

    quoted = _quote_posix("abc\n")

    assert quoted == "'abc\n'"
    assert quoted.startswith("'") and quoted.endswith("'")


def test_env_emits_powershell_escaped_assignments(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {"greeting": 'say "hi" `there`'}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    _enable_sandbox(
        tmp_project, "  env:\n    greeting: 'say \"hi\" `there`'\n"
    )

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha", shell="powershell"))

    out = capsys.readouterr().out
    assert '$env:greeting = "say `"hi`" ``there``"' in out


def test_env_emits_json(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    _enable_sandbox(tmp_project)

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha", shell="json"))

    assert json.loads(capsys.readouterr().out)["COMPOSE_PROJECT_NAME"] == "feat-alpha"


def test_env_without_state_exits_nonzero(tmp_project, stub_docker):
    with pytest.raises(SystemExit) as excinfo:
        cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/none"))
    assert excinfo.value.code != 0


def test_misplaced_flag_after_action_fails_closed(tmp_project, stub_docker, capsys):
    """`sandbox status --dir X` is what real argparse produces when a flag is
    placed after the action: `action` (REMAINDER's positional match) with
    the swallowed tokens landing in `cmd` and `dir` left at its default. This
    must never be silently accepted -- it means the config the rest of
    `cmd_sandbox` loads is not the one the user asked for.
    """
    args = _args("status", cmd=["--dir", str(tmp_project)])

    with pytest.raises(SystemExit) as excinfo:
        cmd_sandbox(args)

    assert excinfo.value.code != 0
    out = capsys.readouterr().out
    assert "status" in out
    assert "--dir" in out
    assert tmp_project.name in out
    assert "BEFORE the action" in out


def test_exec_action_is_unaffected_by_the_misplaced_flag_guard(tmp_project, stub_docker):
    """`exec`'s trailing `cmd` is its whole point and must still pass through."""
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    _enable_sandbox(tmp_project)

    with pytest.raises(SystemExit) as excinfo:
        cmd_sandbox(_args(
            "exec", dir=str(tmp_project), branch="feat/alpha",
            cmd=["--", sys.executable, "-c", "pass"],
        ))

    assert excinfo.value.code == 0


def test_teardown_refuses_volume_destruction_without_yes(tmp_project, stub_docker):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    before = len(stub_docker.calls)

    with pytest.raises(SystemExit):
        cmd_sandbox(_args("teardown", dir=str(tmp_project), branch="feat/alpha"))

    assert len(stub_docker.calls) == before, "destroyed volumes without --yes"
    assert sandbox.read_state(tmp_project, "feat/alpha") is not None
