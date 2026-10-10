"""Neo4j 实现:懒加载官方驱动,本步骤只做生命周期与健康检查(查询方法随步骤补齐)。

要点(§6/§7/§11):
- `neo4j` 驱动**只在 connect() 里 import**:总开关关闭时永不执行到这里,
  依赖没装 / 未配置也不影响启动(connect 返回 False 并记原因);
- 连接与探活都是同步阻塞调用,由调用方放进线程(anyio.to_thread),不阻塞事件循环;
- **绝不抛**:一切失败折成 {available: False, reason: ...} —— 图是软依赖,
  失败要显式降级(health / 诊断里看得见),但不拖垮聊天与启动;
- 凭据只在连接时使用,不进日志、不进 health(health 只回显 uri / database 这类非敏感项);
- 版本锚点:驱动版本在 requirements.txt 固定区间(neo4j>=5.24,<6),不使用未声明特性,
  Cypher 全部参数化(后续查询方法同样遵守)。
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.memory.graph.base import GraphClient, GraphSwitches, resolve_switches

logger = logging.getLogger(__name__)


class Neo4jGraphClient(GraphClient):
    name = "neo4j"

    def __init__(self, settings: Settings) -> None:
        self.switches: GraphSwitches = resolve_switches(settings)
        self._uri = str(settings.neo4j_uri or "").strip()
        self._username = str(settings.neo4j_username or "").strip()
        self._password = str(settings.neo4j_password or "")
        self._database = str(settings.neo4j_database or "neo4j").strip() or "neo4j"
        self._connect_timeout = float(settings.memory_graph_connect_timeout_seconds)
        self._query_timeout = float(settings.memory_graph_query_timeout_seconds)
        self._max_pool = int(settings.memory_graph_max_pool_size)
        self._driver = None
        self._available = False
        self._error = ""

    # ---- 生命周期 ----

    def connect(self) -> bool:
        """建驱动 + 探活(同步阻塞;绝不抛)。已连接时直接返回状态。"""
        if self._available and self._driver is not None:
            return True
        if not self._uri:
            return self._fail("未配置 NEO4J_URI")
        if not self._password:
            # 先查配置再 import 驱动:缺配置时连依赖都不需要
            return self._fail("未配置 NEO4J_PASSWORD")
        try:
            from neo4j import GraphDatabase
        except Exception as exc:  # 依赖没装(生产漏装 / 本地未装):明确失败,不装死
            return self._fail(f"neo4j 驱动不可用({type(exc).__name__})")

        driver = None
        try:
            driver = GraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password),
                connection_timeout=self._connect_timeout,
                max_connection_pool_size=self._max_pool,
            )
            driver.verify_connectivity()
        except Exception as exc:
            # 保留错误阶段与异常类型(§18);异常文本不含口令,但只记一次警告级别
            if driver is not None:
                try:
                    driver.close()
                except Exception:
                    pass
            return self._fail(f"连接失败({type(exc).__name__})")
        self._driver = driver
        self._available = True
        self._error = ""
        logger.info("记忆图:Neo4j 已连接(%s,库 %s)", self._uri, self._database)
        return True

    def health(self) -> dict:
        return {
            "client": self.name,
            "enabled": True,
            "available": self._available,
            "reason": self._error if not self._available else "",
            "uri": self._uri,
            "database": self._database,
        }

    def publish(self, data: dict) -> None:
        """Atomically replace one owner's projection; callers hold its SQL fence."""
        if not self.connect():
            raise RuntimeError("Neo4j unavailable")
        from neo4j import Query
        owner = data["user_id"] + ":" + data["scope"]
        nodes = []
        edges = []
        def node(kind, row):
            props = {k: v for k, v in row.items() if v is not None
                     and isinstance(v, (str, int, float, bool))}
            props.update(owner=owner, user_id=data["user_id"], scope=data["scope"],
                         generation=data["generation"], kind=kind)
            nodes.append({"key": owner + ":" + kind + ":" + row["id"], "props": props})
        for kind, rows in (("entity", data["entities"]), ("relation", data["relations"]),
                           ("event", data["events"]), ("memory", data["memories"])):
            for row in rows:
                node(kind, row)
        def edge(a_kind, a, b_kind, b, role):
            if a and b:
                edges.append({"a": owner + ":" + a_kind + ":" + a,
                              "b": owner + ":" + b_kind + ":" + b, "role": role})
        for row in data["relations"]:
            edge("entity", row["subject_entity_id"], "relation", row["id"], "subject")
            edge("relation", row["id"], "entity", row["object_entity_id"], "object")
        for row in data["participants"]:
            edge("entity", row["entity_id"], "event", row["event_id"], row["role"])
        for row in data["links"]:
            edge(row["fact_kind"], row["fact_id"], "memory", row["memory_id"], "evidence")
        for row in data.get("mentions", []):
            edge("entity", row["entity_id"], "memory", row["memory_id"], "mention")
        edges = list({(r["a"], r["b"], r["role"]): r for r in edges}.values())
        with self._driver.session(database=self._database) as session:
            session.run(Query("CREATE CONSTRAINT qa_graph_key IF NOT EXISTS "
                              "FOR (n:QAMemoryGraph) REQUIRE n.key IS UNIQUE",
                              timeout=self._query_timeout)).consume()
            def write(tx):
                def run(q, **params):
                    tx.run(Query(q, timeout=self._query_timeout), **params).consume()
                run("MATCH (n:QAMemoryGraph {owner:$owner}) DETACH DELETE n", owner=owner)
                from app.config import get_settings
                batch = max(1, min(1000, get_settings().memory_graph_sync_batch))
                for offset in range(0, len(nodes), batch):
                    run("UNWIND $rows AS r CREATE (n:QAMemoryGraph) SET n=r.props, n.key=r.key", rows=nodes[offset:offset + batch])
                for offset in range(0, len(edges), batch):
                    run("UNWIND $rows AS r MATCH (a:QAMemoryGraph {key:r.a}), "
                        "(b:QAMemoryGraph {key:r.b}) CREATE (a)-[:QA_LINK {role:r.role}]->(b)", rows=edges[offset:offset + batch])
                if nodes:
                    run("CREATE (n:QAMemoryGraph {key:$key,owner:$owner,kind:'manifest',digest:$digest})",
                        key=owner + ":manifest", owner=owner, digest=data["digest"])
            session.execute_write(write)

    def recall(self, data: dict, *, query: str) -> dict:
        """Bounded BFS; structural paths are evidence links, not inferred claims."""
        from collections import deque
        from neo4j import Query
        from app.config import get_settings
        import time
        deadline = time.monotonic() + self._query_timeout
        settings = get_settings()
        owner = data["user_id"] + ":" + data["scope"]
        aliases = data.get("aliases", [])
        matches = []
        for e in data["entities"]:
            names = [e["name"], *[a["alias"] for a in aliases if a["entity_id"] == e["id"]]]
            if any(n and n.casefold() in query.casefold() for n in names):
                matches.append((e["id"], e["name"]))
        matches = matches[:max(1, settings.memory_graph_max_entities)]
        paths, visited = [], set()
        fanout_clipped = 0
        max_nodes = max(1, settings.memory_graph_max_nodes)
        max_paths = max(1, settings.memory_graph_max_paths)
        fanout = max(1, settings.memory_graph_fanout_limit)
        max_depth = max(1, min(4, settings.memory_graph_max_hops)) * 2 + 1
        if not data["memories"]:
            return {"enabled": True, "candidates": [], "paths": [], "digest": data["digest"]}
        with self._driver.session(database=self._database) as session:
            def read(q, **params):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("图召回超过总时间预算")
                return list(session.run(Query(q, timeout=remaining), **params))
            marker = read("MATCH (n:QAMemoryGraph {key:$key}) RETURN n.digest AS digest", key=owner + ":manifest")
            if not marker or marker[0]["digest"] != data["digest"]:
                raise RuntimeError("图投影版本与 MySQL 不一致")
            queue = deque([(owner + ":entity:" + eid, [{"id": eid, "kind": "entity"}], name)
                           for eid, name in matches])
            while queue and len(visited) < max_nodes and len(paths) < max_paths:
                key, path, name = queue.popleft()
                if key in visited:
                    continue
                visited.add(key)
                neighbors = read("MATCH (a:QAMemoryGraph {key:$key})-[r:QA_LINK]-(b:QAMemoryGraph) "
                    "WHERE b.owner=$owner RETURN b.key AS key,b.id AS id,b.kind AS kind,r.role AS role "
                    "ORDER BY b.key LIMIT $limit", key=key, owner=owner, limit=fanout + 1)
                fanout_clipped += int(len(neighbors) > fanout)
                for row in neighbors[:fanout]:
                    nxt = [*path, {"id": row["id"], "kind": row["kind"], "role": row["role"]}]
                    if row["kind"] == "memory":
                        fact_ids = {n["id"] for n in path if n["kind"] in ("relation", "event")}
                        members = [l["memory_id"] for l in data["links"] if l["fact_id"] in fact_ids]
                        paths.append({"entity": name, "nodes": nxt,
                                      "memory_ids": list(dict.fromkeys([*members, row["id"]]))})
                        if len(paths) >= max_paths:
                            break
                    elif len(nxt) <= max_depth and row["key"] not in visited:
                        queue.append((row["key"], nxt, name))
            # A concurrent replace between queries must invalidate the whole pass.
            marker = read("MATCH (n:QAMemoryGraph {key:$key}) RETURN n.digest AS digest", key=owner + ":manifest")
            if not marker or marker[0]["digest"] != data["digest"]:
                raise RuntimeError("图召回期间投影发生变化")
        candidates = list(dict.fromkeys(mid for p in paths for mid in p["memory_ids"]))
        return {"enabled": True, "matched_entities": matches, "paths": paths,
                "nodes_visited": len(visited), "fanout_clipped": fanout_clipped,
                "budget_exhausted": bool(queue) or len(paths) >= max_paths or fanout_clipped > 0,
                "candidates": candidates[:settings.memory_graph_candidate_k], "digest": data["digest"]}

    def audit(self, data: dict) -> dict:
        """Check every projected scalar property and edge, not just totals."""
        from neo4j import Query
        owner = data["user_id"] + ":" + data["scope"]
        expected = {}
        expected_edges = set()
        for kind, rows in (("entity", data["entities"]), ("relation", data["relations"]),
                           ("event", data["events"]), ("memory", data["memories"])):
            for row in rows:
                props = {k: v for k, v in row.items() if v is not None and isinstance(v, (str, int, float, bool))}
                key = owner + ":" + kind + ":" + row["id"]
                props.update(owner=owner, user_id=data["user_id"], scope=data["scope"],
                             generation=data["generation"], kind=kind, key=key)
                expected[key] = props
        key = owner + ":manifest"
        if expected:
            expected[key] = {"key": key, "owner": owner, "kind": "manifest", "digest": data["digest"]}
        def edge(ak, a, bk, b, role):
            if a and b:
                expected_edges.add((owner + ":" + ak + ":" + a, owner + ":" + bk + ":" + b, role))
        for r in data["relations"]:
            edge("entity", r["subject_entity_id"], "relation", r["id"], "subject")
            edge("relation", r["id"], "entity", r["object_entity_id"], "object")
        for r in data["participants"]:
            edge("entity", r["entity_id"], "event", r["event_id"], r["role"])
        for r in data["links"]:
            edge(r["fact_kind"], r["fact_id"], "memory", r["memory_id"], "evidence")
        for r in data.get("mentions", []):
            edge("entity", r["entity_id"], "memory", r["memory_id"], "mention")
        with self._driver.session(database=self._database) as session:
            actual = {r["key"]: dict(r["props"]) for r in session.run(Query(
                "MATCH (n:QAMemoryGraph {owner:$owner}) RETURN n.key AS key,properties(n) AS props",
                timeout=self._query_timeout), owner=owner)}
            actual_edges = set((r["a"], r["b"], r["role"]) for r in session.run(Query(
                "MATCH (a:QAMemoryGraph {owner:$owner})-[r:QA_LINK]->(b) "
                "RETURN a.key AS a,b.key AS b,r.role AS role", timeout=self._query_timeout), owner=owner))
        bad = [key for key, props in expected.items() if actual.get(key) != props]
        extra = set(actual) - set(expected)
        return {"clean": not bad and not extra and actual_edges == expected_edges,
                "mismatched_nodes": len(bad), "orphan_nodes": len(extra),
                "missing_edges": len(expected_edges - actual_edges),
                "orphan_edges": len(actual_edges - expected_edges)}

    def close(self) -> None:
        """释放驱动(幂等;绝不抛)。关掉后可以再 connect()。"""
        driver, self._driver = self._driver, None
        self._available = False
        if driver is None:
            return
        try:
            driver.close()
        except Exception as exc:
            logger.warning("记忆图:关闭 Neo4j 驱动失败(%s)", type(exc).__name__)

    # ---- 内部 ----

    def _fail(self, reason: str) -> bool:
        self._error = reason
        self._available = False
        return False
