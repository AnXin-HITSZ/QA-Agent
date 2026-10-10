"""Startup orchestration without Docker, SSH or real service connections."""
from types import SimpleNamespace
import pytest
from scripts import neo4j_setup as setup


def settings(**kwargs):
    values = dict(memory_graph_enabled=True, neo4j_uri="bolt://127.0.0.1:7687",
                  neo4j_username="neo4j", neo4j_password="test-$-password!#")
    values.update(kwargs)
    return SimpleNamespace(**values)


def client(monkeypatch, ok=True):
    from app.memory import graph
    state = {"closed": False, "connections": 0}
    class Fake:
        def connect(self):
            state["connections"] += 1
            return ok
        def close(self):
            state["closed"] = True
    monkeypatch.setattr(graph, "build_graph_client", lambda _: Fake())
    return state


def test_disabled_does_not_read_password_or_run_docker(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("external call")
    setup.deploy(SimpleNamespace(memory_graph_enabled=False), runner=forbidden)
    assert setup.endpoint(SimpleNamespace(memory_graph_enabled=False)) is None


def test_password_passed_only_in_environment_and_connection_verified(monkeypatch, capsys):
    calls = []
    state = client(monkeypatch)
    s = settings()
    setup.deploy(s, runner=lambda args, **kw: calls.append((args, kw)))
    args, kw = calls[0]
    assert s.neo4j_password not in " ".join(args)
    assert kw["env"]["NEO4J_PASSWORD"] == s.neo4j_password
    assert "--wait" in args and kw["check"]
    assert state == {"closed": True, "connections": 1}
    assert s.neo4j_password not in capsys.readouterr().out


def test_remote_checks_auth_without_starting_local_docker(monkeypatch):
    state = client(monkeypatch)
    setup.deploy(settings(neo4j_uri="bolt://graph.internal:7687"),
                 runner=lambda *a, **kw: pytest.fail("local Docker"))
    assert state["closed"]


def test_failed_auth_blocks_restart_and_closes_driver(monkeypatch):
    state = client(monkeypatch, ok=False)
    with pytest.raises(RuntimeError, match="认证连接失败"):
        setup.deploy(settings(), runner=lambda *a, **kw: None)
    assert state["closed"]


@pytest.mark.parametrize("kwargs", [dict(neo4j_password=""),
    dict(neo4j_uri="http://localhost:7474"), dict(neo4j_uri="bolt://u:secret@localhost:7687"),
    dict(neo4j_uri="bolt://localhost:7688"), dict(neo4j_username="someone")])
def test_bad_config_never_launches_docker(kwargs):
    with pytest.raises(ValueError):
        setup.deploy(settings(**kwargs), runner=lambda *a, **kw: pytest.fail("Docker called"))
