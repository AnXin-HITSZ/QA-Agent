"""图记忆开关与生命周期(§6/§7):禁用 ⇒ 零连接、矩阵收敛、失败不抛、凭据不进 health。

这三条是后面所有图工作的地基:任何一步都不允许在总开关关闭时 import 驱动 / 建连接,
也不允许把「关闭」说成「故障」、把失败说成成功。全部离线,不依赖任何真实 Neo4j。
"""

from __future__ import annotations

import json
import socket
import sys

from app.config import Settings
from app.memory import graph as graph_pkg
from app.memory.graph import (
    DisabledGraphClient, Neo4jGraphClient, build_graph_client, resolve_switches,
)


def _settings(**kw) -> Settings:
    """只关心图相关的字段;其余取默认(与运行时同一套 Settings 模型)。"""
    base = dict(
        memory_graph_enabled=False,
        memory_graph_write_enabled=False,
        memory_graph_search_enabled=False,
        memory_graph_worker_enabled=False,
        neo4j_uri="bolt://127.0.0.1:1",     # 必然连不上的端口,防手滑连到真服务
        neo4j_username="neo4j",
        neo4j_password="",
        neo4j_database="neo4j",
    )
    base.update(kw)
    return Settings(**base)


def test_master_off_is_disabled_and_never_touches_driver(monkeypatch):
    """全关(默认):禁用实现 + 不 import 驱动 + 连 socket 层都不许有动作。"""
    sys.modules.pop("neo4j", None)  # 让「驱动从未被加载」可被断言
    client = build_graph_client(_settings())
    assert isinstance(client, DisabledGraphClient)
    st = client.health()
    assert st == {"client": "disabled", "enabled": False, "available": False,
                  "reason": st["reason"]}
    assert "未启用" in st["reason"]

    def _boom(*_a, **_k):
        raise AssertionError("总开关关闭时不允许发起任何连接")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    assert client.connect() is False
    client.close()                       # 幂等、无副作用
    client.close()
    assert "neo4j" not in sys.modules


def test_switch_matrix_subordinate_to_master():
    """子开关一律受总开关约束;总开关开、子开关关 = 「只建图不召回」等的合法组合。"""
    rows = [
        # master, write, search, worker → 期望有效值
        (False, True, True, True, (False, False, False, False)),   # 子开关单独开无效
        (True, False, False, False, (True, False, False, False)),  # 全子关:仅装配不动作
        (True, True, False, True, (True, True, False, True)),      # 写 + 后台,不召回
        (True, False, True, False, (True, False, True, False)),    # 读固定图数据,不新增
        (True, True, True, True, (True, True, True, True)),        # 全开
    ]
    for master, write, search, worker, want in rows:
        sw = resolve_switches(_settings(memory_graph_enabled=master,
                                        memory_graph_write_enabled=write,
                                        memory_graph_search_enabled=search,
                                        memory_graph_worker_enabled=worker))
        assert (sw.master, sw.write, sw.search, sw.worker) == want, (master, write, search, worker)


def test_enabled_without_password_fails_closed_without_driver_import():
    """总开关开但缺凭据:明确失败、不抛、不漏口令;缺配置时连驱动都不加载。"""
    sys.modules.pop("neo4j", None)
    client = build_graph_client(_settings(memory_graph_enabled=True,
                                          memory_graph_write_enabled=True))
    assert isinstance(client, Neo4jGraphClient)
    assert client.connect() is False
    st = client.health()
    assert st["enabled"] is True and st["available"] is False
    assert "NEO4J_PASSWORD" in st["reason"]
    assert "neo4j" not in sys.modules
    client.close()
    client.close()


class _FakeDriver:
    """离线替身:记录 verify / close 调用,可编排失败。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.verified = False
        self.closed = False

    def verify_connectivity(self) -> None:
        self.verified = True
        if self.fail is not None:
            raise self.fail

    def close(self) -> None:
        self.closed = True


def _install_fake_driver(monkeypatch, driver: _FakeDriver) -> dict:
    """把 neo4j.GraphDatabase.driver(...) 换成假驱动,并捕获传入参数。"""
    import neo4j

    seen: dict = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, **kwargs):
            seen["uri"] = uri
            seen.update(kwargs)
            return driver

    monkeypatch.setattr(neo4j, "GraphDatabase", _FakeGraphDatabase)
    return seen


def test_connect_success_records_availability_and_close_is_idempotent(monkeypatch):
    driver = _FakeDriver()
    seen = _install_fake_driver(monkeypatch, driver)
    client = build_graph_client(_settings(memory_graph_enabled=True,
                                          memory_graph_search_enabled=True,
                                          neo4j_password="secret-placeholder"))
    assert client.connect() is True
    assert driver.verified is True
    assert seen["auth"] == ("neo4j", "secret-placeholder")
    assert seen["connection_timeout"] == 5.0            # 超时/池配置确实传给了驱动
    assert seen["max_connection_pool_size"] == 10
    st = client.health()
    assert st["available"] is True and st["reason"] == ""
    assert "secret-placeholder" not in json.dumps(st)   # 凭据不进 health(§11)
    assert client.connect() is True                      # 已连接时不再重复建驱动
    client.close()
    assert driver.closed is True
    client.close()                                       # 幂等
    assert client.health()["available"] is False         # 关掉后状态如实反映


def test_connect_failure_is_reported_not_raised(monkeypatch):
    driver = _FakeDriver(fail=RuntimeError("boom"))
    _install_fake_driver(monkeypatch, driver)
    client = build_graph_client(_settings(memory_graph_enabled=True,
                                          memory_graph_write_enabled=True,
                                          neo4j_password="secret-placeholder"))
    assert client.connect() is False
    st = client.health()
    assert st["available"] is False
    assert "RuntimeError" in st["reason"]                # 保留异常类型(§18)
    assert "secret-placeholder" not in json.dumps(st)
    assert driver.closed is True                         # 失败的驱动要被关掉,不悬挂


def test_process_singleton_follows_settings_and_resets(monkeypatch):
    graph_pkg.reset_graph_client()
    try:
        monkeypatch.setattr(graph_pkg, "get_settings", lambda: _settings())
        first = graph_pkg.get_graph_client()
        assert isinstance(first, DisabledGraphClient)
        assert graph_pkg.get_graph_client() is first     # 单例
        graph_pkg.reset_graph_client()
        assert graph_pkg.get_graph_client() is not first
    finally:
        graph_pkg.reset_graph_client()
