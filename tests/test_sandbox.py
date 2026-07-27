import errno
import socket
import time

import pytest

from scripts.errors import OrchestratorError
from scripts import sandbox


def test_project_name_follows_compose_rules():
    assert sandbox.project_name_for("feat/Alpha_1") == "feat-alpha_1"
    assert sandbox.project_name_for("feat//weird--branch--") == "feat-weird-branch"
    assert sandbox.project_name_for("") == "default"


def test_ip_for_is_deterministic_and_never_loopback_one(monkeypatch):
    monkeypatch.setattr(sandbox, "_probe", lambda ip, port=0: "free")
    first = sandbox.ip_for("feat/alpha")
    second = sandbox.ip_for("feat/alpha")
    assert first == second
    assert first != "127.0.0.1"
    assert first.startswith("127.0.0.")


def test_ip_for_skips_addresses_reported_busy(monkeypatch):
    """A branch whose hashed octet is taken must move on, not fail."""
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(
        sandbox, "_probe", lambda ip, port=0: "busy" if ip == taken else "free"
    )
    assert sandbox.ip_for("feat/alpha") != taken


def test_ip_for_skips_addresses_the_caller_declares_busy(monkeypatch):
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(sandbox, "_probe", lambda ip, port=0: "free")
    assert sandbox.ip_for("feat/alpha", busy=[taken]) != taken


def test_unavailable_address_aborts_with_a_remedy_not_a_busy_report(monkeypatch):
    """EADDRNOTAVAIL is a platform fact. Scanning 254 more is the wrong answer."""
    probes = []

    def fake_probe(ip, port=0):
        probes.append(ip)
        return "unavailable"

    monkeypatch.setattr(sandbox, "_probe", fake_probe)

    with pytest.raises(OrchestratorError) as excinfo:
        sandbox.ip_for("feat/alpha")

    message = str(excinfo.value)
    assert "ifconfig" in message, "the error must name the remediation command"
    assert "no free loopback" not in message.lower()
    assert len(probes) == 1, f"aborted after {len(probes)} probes, expected 1"


def test_probe_maps_errnos_to_verdicts(monkeypatch):
    class FakeSocket:
        def __init__(self, code):
            self._code = code

        def bind(self, _address):
            if self._code is None:
                return None
            raise OSError(self._code, "fake")

        def close(self):
            return None

    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRNOTAVAIL)
    )
    assert sandbox._probe("127.0.0.9") == "unavailable"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRINUSE))
    assert sandbox._probe("127.0.0.9") == "busy"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(None))
    assert sandbox._probe("127.0.0.9") == "free"


def test_state_round_trips(tmp_path):
    record = sandbox.SandboxState(
        branch="feat/alpha", ip="127.0.0.7",
        project_name="feat-alpha", started_at="2026-07-27T00:00:00+00:00",
    )
    sandbox.write_state(tmp_path, record)
    assert sandbox.read_state(tmp_path, "feat/alpha") == record


def test_read_state_is_none_when_untracked(tmp_path):
    assert sandbox.read_state(tmp_path, "feat/nothing") is None


def test_clear_state_is_idempotent(tmp_path):
    sandbox.clear_state(tmp_path, "feat/absent")  # must not raise
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    sandbox.clear_state(tmp_path, "feat/a")
    assert sandbox.read_state(tmp_path, "feat/a") is None


def test_list_states_ignores_unparsable_files(tmp_path):
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    (sandbox_state_dir := sandbox.sandbox_dir(tmp_path)).mkdir(exist_ok=True)
    (sandbox_state_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    assert sandbox.list_states(tmp_path) == [record]


def test_render_env_injects_the_contract_variables():
    rendered = sandbox.render_env({"env": {}}, "127.0.0.7", "feat-a")
    assert rendered["LOOPBACK_IP"] == "127.0.0.7"
    assert rendered["COMPOSE_PROJECT_NAME"] == "feat-a"


def test_render_env_substitutes_both_tokens():
    rendered = sandbox.render_env(
        {"env": {"dsn": "postgres://{ip}:5432/{project}"}}, "127.0.0.7", "feat-a"
    )
    assert rendered["dsn"] == "postgres://127.0.0.7:5432/feat-a"


def test_render_env_expands_process_environment(monkeypatch):
    """A project with a real secret sources it from the environment."""
    monkeypatch.setenv("SANDBOX_TEST_PASSWORD", "s3cret")
    rendered = sandbox.render_env(
        {"env": {"dsn": "postgres://u:${SANDBOX_TEST_PASSWORD}@{ip}/db"}},
        "127.0.0.7", "feat-a",
    )
    assert rendered["dsn"] == "postgres://u:s3cret@127.0.0.7/db"


def _sandbox_config(**overrides):
    cfg = {
        "enabled": True,
        "compose_file": "docker-compose.yml",
        "health_service": None,
        "health_timeout": 5,
        "env": {"dsn": "postgres://{ip}:5432/db"},
        "teardown": {"on_verified_closed": "volumes", "on_failed": "containers"},
    }
    cfg.update(overrides)
    return {"sandbox": cfg}


def test_ensure_up_addresses_the_branch_it_was_given(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    env = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    assert env["COMPOSE_PROJECT_NAME"] == "feat-alpha"
    assert env["dsn"] == f"postgres://{env['LOOPBACK_IP']}:5432/db"
    argv = stub_docker.argv_of(0)
    assert argv[:2] == ["compose", "-p"]
    assert argv[2] == "feat-alpha"
    assert argv[-2:] == ["up", "-d"]
    assert stub_docker.calls[0]["loopback_ip"] == env["LOOPBACK_IP"]


def test_ensure_up_is_idempotent_on_the_address(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    first = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    second = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    assert first["LOOPBACK_IP"] == second["LOOPBACK_IP"]


def test_ensure_up_is_inert_when_disabled(tmp_path, stub_docker):
    assert sandbox.ensure_up("feat/a", tmp_path, _sandbox_config(enabled=False)) == {}
    assert stub_docker.calls == []


def test_ensure_up_fails_closed_without_a_compose_file(tmp_path, stub_docker):
    with pytest.raises(OrchestratorError, match="docker-compose.yml"):
        sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())


def test_ensure_up_fails_closed_when_compose_fails(tmp_path, stub_docker, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("SUPERPOWERS_DOCKER_EXIT", "1")
    with pytest.raises(OrchestratorError):
        sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())


def test_resolve_env_has_no_side_effects(tmp_path, stub_docker):
    assert sandbox.resolve_env("feat/alpha", tmp_path, _sandbox_config()) == {}
    assert stub_docker.calls == []


def test_teardown_containers_keeps_state_and_omits_dash_v(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "containers")

    argv = stub_docker.argv_of(-1)
    assert argv[-1] == "down"
    assert "-v" not in argv
    assert sandbox.read_state(tmp_path, "feat/alpha") is not None


def test_teardown_volumes_destroys_state(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "volumes")

    assert stub_docker.argv_of(-1)[-2:] == ["down", "-v"]
    assert sandbox.read_state(tmp_path, "feat/alpha") is None


def test_health_gate_blocks_when_the_service_never_reports_healthy(
    tmp_path, stub_docker, monkeypatch
):
    """An agent dispatched at a stack that is not ready fails on its first
    connection, and the reason surfaces only in the agent's own log. Refuse
    at dispatch instead."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # The stub only prints a healthy record when --format is present; drop the
    # marker it looks for so `ps` never reports healthy.
    monkeypatch.setattr(sandbox, "_compose", _never_healthy(sandbox._compose))

    with pytest.raises(OrchestratorError, match="healthy"):
        sandbox.ensure_up(
            "feat/alpha", tmp_path,
            _sandbox_config(health_service="postgres", health_timeout=1),
        )


def _never_healthy(real_compose):
    def wrapper(project_root, cfg, state, args, env, capture=False):
        if capture:
            class Result:
                stdout = '{"Service": "postgres", "Health": "starting"}'
                returncode = 0
            return Result()
        return real_compose(project_root, cfg, state, args, env, capture)
    return wrapper


def test_health_gate_surfaces_the_real_error_when_the_probe_itself_fails(
    tmp_path, stub_docker, monkeypatch
):
    """A broken `docker compose ps` (bad service name, unreachable daemon,
    malformed compose file) must not be indistinguishable from a container
    that is merely slow to start: it should fail immediately, with the real
    stderr, not after the full health_timeout with the generic message."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox, "_compose", _broken_probe(sandbox._compose)
    )

    start = time.monotonic()
    with pytest.raises(OrchestratorError, match="boom: no such service") as exc_info:
        sandbox.ensure_up(
            "feat/alpha", tmp_path,
            # A long timeout: if the bug regresses, this test would hang
            # until it elapses instead of failing fast.
            _sandbox_config(health_service="postgres", health_timeout=30),
        )
    elapsed = time.monotonic() - start

    assert elapsed < 5, "should fail immediately, not wait out health_timeout"
    assert "did not report healthy" not in str(exc_info.value)


def _broken_probe(real_compose):
    def wrapper(project_root, cfg, state, args, env, capture=False):
        if capture:
            class Result:
                stdout = ""
                stderr = "boom: no such service\n"
                returncode = 1
            return Result()
        return real_compose(project_root, cfg, state, args, env, capture)
    return wrapper


def test_health_gate_passes_when_the_service_is_healthy(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env = sandbox.ensure_up(
        "feat/alpha", tmp_path,
        _sandbox_config(health_service="postgres", health_timeout=5),
    )
    assert env["LOOPBACK_IP"].startswith("127.0.0.")


def test_teardown_none_touches_nothing(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    before = len(stub_docker.calls)

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "none")

    assert len(stub_docker.calls) == before
    assert sandbox.read_state(tmp_path, "feat/alpha") is not None


def test_allocation_is_serialised(tmp_path, monkeypatch):
    """A second allocator must not run while the first holds the lock."""
    from scripts.locks import acquire_slice_lock

    held = acquire_slice_lock(sandbox._ALLOC_LOCK_ID, tmp_path)
    try:
        with pytest.raises(OrchestratorError, match="allocation lock"):
            with sandbox._allocation_lock(tmp_path, attempts=2, delay=0.01):
                pass
    finally:
        held.unlink(missing_ok=True)


def test_allocation_lock_is_released_on_the_error_path(tmp_path):
    from scripts.locks import acquire_slice_lock

    with pytest.raises(RuntimeError):
        with sandbox._allocation_lock(tmp_path):
            raise RuntimeError("boom")

    # If the lock leaked, this second acquisition would raise LockError.
    acquire_slice_lock(sandbox._ALLOC_LOCK_ID, tmp_path).unlink(missing_ok=True)
