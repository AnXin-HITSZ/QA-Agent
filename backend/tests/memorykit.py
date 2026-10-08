"""长期记忆的测试道具:可控的两路召回替身与离线记忆环境装配。

与 meterkit / ocrkit 同类:只服务测试,不进应用代码。

为什么稠密召回要换成替身:真 Qdrant 的名次由**向量相似度**决定,而离线假 Embeddings
只能给出确定性向量(内容哈希),没法让测试安排「谁必须排在谁前面」。所以检索编排的用例
统一用脚本化名次 —— 断言的是「拿到名次之后我们做了什么」,而不是向量库本身的质量。
重排序同理(它要真调用付费接口)。
"""

from __future__ import annotations

from types import SimpleNamespace

from qdrant_client import QdrantClient

from app.memory import repo, search, service, vector
from app.memory.extract import ExtractedFact
from app.memory.models import SCOPE_FORMAL
from tests.conftest import FakeEmbeddings


def hits(ids, *, skew: dict[str, dict] | None = None) -> list[SimpleNamespace]:
    """把一串 memory_id 变成「Qdrant 命中」:点 id 按版本化规则生成,payload 取自当前行。

    行不存在(已被删)时只带 memory_id —— 那正是真实索引里残留的孤儿点,交给校验层去挡。
    `skew` 用来手工改某条命中的 payload 字段(模拟「索引还停在旧版本」)。
    """
    from app.memory import db, repo

    with db.session_scope() as session:
        rows = {item.id: item for item in repo.list_by_ids(session, list(ids))}
    out = []
    for mid in ids:
        item = rows.get(mid)
        payload = dict(vector.payload_of(item)) if item is not None else {"memory_id": mid}
        payload.update((skew or {}).get(mid, {}))
        revision = int(payload.get("revision") or 0)
        out.append(SimpleNamespace(id=vector.point_id(mid, revision), payload=payload))
    return out


def sparse_missing(monkeypatch) -> None:
    """把稀疏(关键词)通道改成「不可用」:考察检索层退回内存 BM25 的降级路径。"""
    monkeypatch.setattr(vector, "search_sparse", lambda *args, **kwargs: None)


class Dense:
    """稠密召回替身:按脚本给名次(顺序即排名),fail 模拟这条链路不可用。

    命中带**真实 payload**(从当前行取 vector.payload_of):检索层现在会拿命中版本与
    MySQL 当前事实逐项比对,替身若只回一个 memory_id,所有候选都会被判成不新鲜 ——
    那样测出来的就不是检索编排了。要专门考察「索引里是这样一条旧点」时,用 skew 改字段。
    """

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.fail: BaseException | None = None
        self.calls: list[dict] = []
        self.skew: dict[str, dict] = {}       # 按 memory_id 覆盖 payload 字段(模拟旧点)

    def __call__(self, query_vector, *, user_id: str, scope: str, top_k: int,
                 embedding_version: str | None = None):
        self.calls.append({"user_id": user_id, "scope": scope, "top_k": top_k,
                           "vector": query_vector, "embedding_version": embedding_version})
        if self.fail is not None:
            raise self.fail
        return hits(self.ids[: max(1, int(top_k))], skew=self.skew)


class FakeRerank:
    """重排序替身:enabled 控制「是否配置齐全」,order 是输入下标的新顺序。"""

    def __init__(self) -> None:
        self.enabled = False
        self.order: list[int] | None = None
        self.scores: dict[int, float] = {}
        self.fail: str = ""
        self.raise_: BaseException | None = None
        self.calls: list[dict] = []

    def configured(self) -> bool:
        return self.enabled

    def config_note(self) -> str:
        return "重排序已关闭(RERANK_ENABLED=false),本次用 RRF 融合结果"

    def rerank(self, query, documents, *, top_n=None, instruct=""):
        self.calls.append({"query": query, "documents": list(documents), "top_n": top_n,
                           "instruct": instruct})
        if self.raise_ is not None:
            raise self.raise_
        if self.fail:
            return SimpleNamespace(order=[], scores={}, ignored=0, error=self.fail, ok=False)
        order = list(self.order if self.order is not None else range(len(documents)))
        scores = dict(self.scores)
        # 真实 RerankOutcome 的不变量:order 里每个下标都有分数(解析层就是这样保证的)
        for rank, index in enumerate(order):
            scores.setdefault(index, 1.0 - rank / 100.0)
        return SimpleNamespace(order=order, scores=scores, ignored=0, error="", ok=True)


def install(monkeypatch, *, settings, dim: int, params: dict | None = None,
            rerank_enabled: bool = False) -> SimpleNamespace:
    """装一套离线记忆环境:真表 + 内存 Qdrant + 假 Embeddings + 脚本化稠密 / 重排。

    `params` 是检索参数的覆盖(小数值便于观察「谁进了最终结果」);调用方再按需改自己的
    配置。返回的命名空间带 dense / rerank 两个替身与 settings。
    """
    from app.rag import store

    client = QdrantClient(location=":memory:")
    embeddings = FakeEmbeddings(dim)
    dense = Dense()
    fake_rerank = FakeRerank()
    fake_rerank.enabled = rerank_enabled

    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(service, "get_embeddings", lambda: embeddings)
    monkeypatch.setattr(search, "get_embeddings", lambda: embeddings)
    monkeypatch.setattr(vector, "search", dense)
    monkeypatch.setattr(search.rerank_client, "configured", fake_rerank.configured)
    monkeypatch.setattr(search.rerank_client, "config_note", fake_rerank.config_note)
    monkeypatch.setattr(search.rerank_client, "rerank", fake_rerank.rerank)

    for name, value in (params or {}).items():
        monkeypatch.setattr(settings, name, value)

    return SimpleNamespace(settings=settings, qdrant=client, embeddings=embeddings,
                           dense=dense, rerank=fake_rerank, repo=repo)


def seed(env, db, *texts: str, user_id: str, scope: str = SCOPE_FORMAL) -> list[str]:
    """写入若干条记忆(走真实写入 + 真实索引),按输入顺序返回 memory_id。

    `scope` 用于评测作用域的隔离用例:写入 / 索引 / 检索全程带同一个作用域。
    """
    result = service.write_facts(user_id=user_id, scope=scope,
                                 facts=[ExtractedFact(text=t, kind="preference") for t in texts])
    assert result.index.deferred == 0 and not result.index.error
    return [item.id for item in result.outcome.added]


class FakeLLM:
    """假聊天模型:按脚本依次返回内容(超出后一直用最后一条),记录每次提交的消息。"""

    def __init__(self, *contents: str, error: BaseException | None = None) -> None:
        from langchain_core.messages import AIMessage

        self._message = AIMessage
        self.script = list(contents) or ["{}"]
        self.error = error
        self.calls: list[list] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self._message(content=self.script[index])

    @property
    def prompts(self) -> list[str]:
        return ["\n".join(getattr(m, "content", "") for m in call) for call in self.calls]
