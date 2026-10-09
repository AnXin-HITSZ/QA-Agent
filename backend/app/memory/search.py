"""混合检索(§8):稠密向量 + BM25 稀疏向量 → MySQL 校验 → RRF 融合 → 重排序 → top_k。

   用户问题
       ├─ 稠密向量召回(Qdrant 命名向量 dense,带 user_id + scope 过滤)
       └─ BM25 稀疏召回(Qdrant 命名向量 bm25,同一集合;服务端 Modifier.IDF)
             → 候选并集,按 memory_id 去重,各自保留排名
             → MySQL 校验:归属 / 状态 / 作用域,以及**向量命中与当前事实逐字段对齐**
               (Qdrant 命中只是候选;dense/sparse 命中要过版本校验,BM25 降级通道不用)
             → RRF 融合(两路各自排名,不把未归一化的分数相加)
             → 保留较大的重排序候选集 → qwen3.7-text-rerank(失败回退,记降级)
             → 最终 top_k → 上下文预算截断

几条刻意的取舍:
- **关键词通道有主备两条**:
  - 主:写入时把正文编成稀疏向量存进 Qdrant(text.sparse_terms,与查询同一编码),
    服务端用 IDF 加权 —— 与稠密向量同一次写入、同一次过滤,不需要每次检索读全量正文;
  - 备:稀疏通道不可用(集合缺稀疏向量 / 服务端不支持 / 集合不存在)时,退回「进程内
    BM25 现算」(语料 = 该用户当前作用域的有效正文)。这条降级路径**显式记在 degraded 里**,
    并如实说明「只覆盖最近 N 条」,不假装关键词检索一切正常。
  两条通道的词项规则与 BM25 参数同一出处(text.py),口径一起版本化(见
  index_version):分词 / 参数一改,embedding_version 变,索引整体重建。
- **BM25 候选不因向量索引状态被丢弃**:候选来自哪一路决定校验口径 —— dense/sparse 命中
  必须与 MySQL 当前事实逐字段对齐(版本 / 摘要),而关键词候选只要归属 / 状态 / 作用域
  对就作数,不要求「它的向量已经建好」。索引落后不该让用户"搜不到自己刚写下的记忆"。
- **两路召回数量、重排序候选数、最终 top_k 分开**(SearchProfile);调用方覆盖 top_k 时,
  重排条数与最终截断一起跟着改,不会出现「按 50 条重排、只返回 3 条」的错配。
- **失败一律降级、绝不抛给聊天**:向量召回不可用 → 只用关键词;重排失败 → 用 RRF 顺序;
  两路都不行 → 返回带 error 的空结果。降级原因写在 degraded 里,调用方可以如实告诉用户。
- **Qdrant 命中不回正文**:正文只从 MySQL 取(Qdrant payload 里本来就没有正文)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.config import get_settings
from app.memory import rerank as rerank_client
from app.memory import text as memory_text
from app.memory import vector
from app.memory.models import SCOPE_FORMAL
from app.metering.context import PURPOSE_MEMORY_SEARCH, bind as bind_context

# 惰性可达的模块级引用:测试里换掉(与 service.py 同一套缝)
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)

TASK_ANSWER = "answer"                # 回答用户问题前的召回
TASK_MAINTENANCE = "maintenance"      # 新旧记忆维护决策的候选召回

CHANNEL_DENSE = "dense"
CHANNEL_SPARSE = "sparse"
CHANNEL_BM25 = "bm25"                 # 降级通道:进程内 BM25(不是 Qdrant 稀疏向量)

# 降级说明(会出现在回来给上层的 degraded 列表里;措辞面向"为什么没做到最好")
DEGRADE_VECTOR_DOWN = "向量召回不可用,本次只用关键词(BM25)召回"
DEGRADE_SPARSE_FALLBACK = "稀疏检索通道不可用,关键词召回已降级为进程内 BM25(只覆盖最近的记忆)"
DEGRADE_BM25_CORPUS_CAPPED = "关键词检索语料达到上限,仅检索了最近的一部分记忆"
DEGRADE_INDEX_LAG_KEYWORD = "部分记忆的向量索引尚未同步,已按正文补入关键词候选"
DEGRADE_RERANK_INPUT_CAPPED = "重排候选超出配置上限,已按融合顺序截断"
DEGRADE_BUDGET = "记忆超出上下文预算,已截断(不保证给全完整正文)"
DEGRADE_STALE_INDEX = "部分候选与当前记忆不一致(索引落后),已排除"


@dataclass(frozen=True)
class SearchProfile:
    """一路检索的参数档位:两路召回数 / 重排候选数 / 最终条数 / 排序说明分开配。"""

    name: str
    vector_k: int
    bm25_k: int
    rerank_k: int
    top_k: int
    rerank_instruct: str = ""


@dataclass
class Evidence:
    """一条给 LLM 阅读的记忆证据(正文来自 MySQL;来源与时间一并带上)。"""

    memory_id: str
    text: str
    score: float
    origin: str                     # rerank / rrf(最终排序依据是谁)
    rrf: float
    vector_rank: int | None
    bm25_rank: int | None
    updated_at: datetime | None
    thread_id: str | None
    revision: int = 0               # 检索时看到的版本:维护决策拿它做落库前的 CAS
    trimmed: bool = False           # 正文是否被预算截断(截断时不伪称完整)
    kind: str = ""                  # 事实类型(提取 / 维护协议里校验过的那个值)
    event_time: datetime | None = None   # 事件发生时间(未知为 None,不推断)
    fact_context: dict = field(default_factory=dict)
    context_text: str = ""
    meta_version: int = 0


@dataclass
class SearchResult:
    query: str
    hits: list[Evidence] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    trace: dict = field(default_factory=dict)  # 显式诊断时保存排名，不进入回答上下文
    error: str = ""                 # 整体不可用(两路都失败)时的原因;非空即没有可用证据

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class _Candidate:
    """一条召回的候选:来自哪一路、排名、以及 Qdrant 给的 payload(内存通道为 None)。"""

    memory_id: str
    rank: int
    channel: str
    payload: dict | None = None


def profile_for(task: str) -> SearchProfile:
    """按任务取参数档位(未知任务按回答检索处理,不猜别的语义)。"""
    s = get_settings()
    if task == TASK_MAINTENANCE:
        return SearchProfile(name=TASK_MAINTENANCE, vector_k=s.memory_maintenance_vector_k,
                             bm25_k=s.memory_maintenance_bm25_k,
                             rerank_k=s.memory_maintenance_rerank_k,
                             top_k=s.memory_maintenance_top_k,
                             rerank_instruct=s.memory_rerank_instruct)
    return SearchProfile(name=TASK_ANSWER, vector_k=s.memory_vector_k, bm25_k=s.memory_bm25_k,
                         rerank_k=s.memory_rerank_k, top_k=s.memory_top_k)


def search(user_id: str, query: str, *, task: str = TASK_ANSWER, top_k: int | None = None,
           scope: str = SCOPE_FORMAL, purpose: str = PURPOSE_MEMORY_SEARCH,
           capture_trace: bool = False) -> SearchResult:
    """检索该用户**在本作用域内**的记忆;环境故障降级而不抛(聊天侧只做最便宜的一次调用)。

    `scope` 默认正式数据:只用正式记忆回答用户问题,评测数据不许混进来(index replay /
    融合 / 校验全程按同一 scope,不是只在一处过滤)。
    """
    q = (query or "").strip()
    if not q:
        return SearchResult(query="")

    profile = profile_for(task)
    limit = int(top_k) if top_k else profile.top_k
    result = SearchResult(query=q)
    if not user_id:
        result.error = "缺少用户标识"
        return result

    dense, dense_note, dense_usable = _dense_candidates(user_id, q, profile, scope=scope,
                                                        purpose=purpose)
    if dense_note:
        result.degraded.append(dense_note)
    keyword, keyword_note, keyword_usable, keyword_sparse = _keyword_candidates(
        user_id, q, profile, scope=scope)
    if keyword_note:
        result.degraded.append(keyword_note)

    snapshots = {}
    try:
        rows, filtered = _validate(user_id, scope=scope,
                                   candidates=[*dense, *keyword],
                                   dense_cap=profile.vector_k, snapshots=snapshots)
    except Exception as exc:  # noqa: BLE001 —— 校对要靠 MySQL;它不可用时同样只能降级
        logger.warning("记忆候选校验不可用(读不到 MySQL):%s:%s", type(exc).__name__, exc)
        result.error = f"记忆检索不可用:候选校验失败({type(exc).__name__})"
        result.counts.update(dense_raw=len(dense), bm25=len(keyword))
        return result
    if filtered.get("filtered_stale"):
        result.degraded.append(f"{DEGRADE_STALE_INDEX}({filtered['filtered_stale']} 条)")
    if filtered.get("filtered_scope"):
        # 不该出现(召回时已按 scope 过滤):出现说明索引里的作用域标错了,如实记一笔
        logger.warning("记忆候选中出现了作用域不符的命中(%d 条),已排除", filtered["filtered_scope"])
    result.counts.update(dense_raw=len(dense), dense=sum(1 for row in rows if row[1]),
                         keyword=len(keyword),
                         keyword_sparse=sum(1 for c in keyword if c.channel == CHANNEL_SPARSE),
                         keyword_supplement=sum(1 for c in keyword if c.channel == CHANNEL_BM25),
                         keyword_channel_sparse=1 if keyword_sparse else 0,
                         union=len(rows), **filtered)
    if not rows:
        if not dense_usable and not keyword_usable:
            # 两路都不可用:这一次真的没有可依据的结果(不是「你没有记忆」)
            result.error = "记忆检索不可用:" + dense_note
        return result

    fused = _rrf(rows, k=int(get_settings().memory_rrf_k))
    trace = result.trace if capture_trace else None
    if trace is not None:
        # 只导出通过权威库归属与版本校验的候选，不泄露被过滤的外部 ID。
        trace["validated"] = [{"memory_id": mid, "vector_rank": dr, "keyword_rank": br}
                              for mid, dr, br in rows]
        trace["fused"] = [{"memory_id": mid, "rank": i, "rrf": score}
                          for i, (mid, score, _, _) in enumerate(fused, 1)]
    ranked, rerank_note = _rerank(user_id, q, fused, profile, limit=limit, purpose=purpose,
                                 snapshots=snapshots, trace=trace)
    if rerank_note:
        result.degraded.append(rerank_note)

    hits, budget_note = _finalize(ranked, limit=limit,
                                  budget=int(get_settings().memory_context_chars), snapshots=snapshots)
    if budget_note:
        result.degraded.append(budget_note)
    result.hits = hits
    if trace is not None:
        trace["ranked"] = [{"memory_id": row[0], "rank": i, "score": row[1], "origin": row[5]}
                           for i, row in enumerate(ranked, 1)]
        trace["final"] = [h.memory_id for h in hits]
    result.counts.update(rerank_in=min(len(fused), profile.rerank_k), final=len(hits),
                         rerank_out=sum(1 for h in hits if h.origin == "rerank"))
    return result


# ---- 两路召回 ----


def _dense_candidates(user_id: str, query: str, profile: SearchProfile, *,
                      scope: str, purpose: str) -> tuple[list[_Candidate], str, bool]:
    """Qdrant 稠密召回;返回 (候选, 降级说明, 这一路是否可用)。

    第三个返回值区分「这条路坏了」和「这条路跑了但没命中」—— 后者是正常结果,
    不该被当成故障报给用户(两路都不坏就只是"没找到相关记忆")。

    多取 `memory_vector_oversample` 条:换模型 / 删除之后索引里会留下不作数的点,
    只按 vector_k 取的话校验一过滤就可能不够用(§8「候选不足用有上限的补取策略」)。
    补取的上限就是这一个数 —— 校验侧再按 vector_k 收口,不会无限放大。
    """
    try:
        with bind_context(purpose=purpose):
            # 查询向量不进缓存:检索不依赖 Redis(与 app/rag/retrieve.py 同口径)
            vector_of_query = get_embeddings().embed_query(query)
        want = profile.vector_k + max(0, int(get_settings().memory_vector_oversample))
        points = vector.search(vector_of_query, user_id=user_id, scope=scope, top_k=want,
                               embedding_version=_current_version())
    except Exception as exc:  # noqa: BLE001 —— 记忆是软依赖:降级要说清楚
        logger.warning("记忆向量召回不可用,降级为关键词召回:%s:%s", type(exc).__name__, exc)
        return [], f"{DEGRADE_VECTOR_DOWN}({type(exc).__name__})", False
    return _points_to_candidates(points, CHANNEL_DENSE), "", True


def _keyword_candidates(user_id: str, query: str, profile: SearchProfile, *,
                        scope: str) -> tuple[list[_Candidate], str, bool, bool]:
    """关键词召回:优先 Qdrant 稀疏通道,不可用时退回进程内 BM25(显式降级)。

    返回 (候选, 降级说明, 这一路是否可用, 是否走的稀疏索引通道)。候选里 `channel` 标着它来自哪条通道
    (sparse = 索引里的稀疏向量; bm25 = 现算),校验口径据此分开(见 _validate)。

    另外**补一笔**:向量索引还没建好的记忆(新建 / 改过还没同步 / 刚换模型)在稀疏通道里
    是找不到的 —— 那些记忆用正文现算 BM25 补进候选,免得「索引落后」变成「用户搜不到
    自己刚说的那句话」。补进来的候选只依赖 MySQL 正文,与索引状态无关。
    """
    points = vector.search_sparse(query, user_id=user_id, scope=scope,
                                  top_k=max(1, profile.bm25_k),
                                  embedding_version=_current_version())
    if points is None:
        cands, reason, usable = _bm25_candidates(user_id, query, profile, scope=scope)
        note = f"{DEGRADE_SPARSE_FALLBACK}({reason})" if reason else DEGRADE_SPARSE_FALLBACK
        return cands, note, usable, False
    cands = _points_to_candidates(points, CHANNEL_SPARSE)
    extra, note = _pending_bm25_supplement(user_id, query, profile, scope=scope,
                                           skip={c.memory_id for c in cands})
    return [*cands, *extra], note, True, True


def _pending_bm25_supplement(user_id: str, query: str, profile: SearchProfile, *,
                             scope: str, skip: set[str]) -> tuple[list[_Candidate], str]:
    """给「索引还没跟上」的记忆补一条关键词通路(§8:BM25 不依赖向量索引状态)。

    待索引集合来自 repo.pending_index_ids(它同时覆盖:向量版本落后 / 正文本版本与索引
    标记不符)。数量有上限,超了如实说明覆盖多少 —— 这条通路是**补充**,不是兜底全量。
    """
    from app.memory import db, repo

    cap = max(1, int(get_settings().memory_bm25_corpus_limit))
    try:
        with db.session_scope() as session:
            ids = repo.pending_index_ids(session, embedding_version=_current_version(),
                                         limit=cap, user_id=user_id, scope=scope)
            if not ids:
                return [], ""
            rows = {item.id: item.text for item in repo.list_by_ids(session, ids)
                    if item.scope == scope and item.user_id == user_id}
    except Exception as exc:  # noqa: BLE001 —— 补充通路失败不影响主通路
        logger.warning("待索引记忆的关键词补充不可用:%s:%s", type(exc).__name__, exc)
        return [], ""
    docs = [(mid, text) for mid, text in rows.items() if mid not in skip]
    if not docs:
        return [], ""
    hits = memory_text.bm25_scores(query, docs)[: max(1, profile.bm25_k)]
    if not hits:
        return [], ""
    note = f"{DEGRADE_INDEX_LAG_KEYWORD}({len(docs)} 条记忆的向量索引尚未同步,已按正文补入关键词候选)"
    return ([_Candidate(memory_id=h.doc_id, rank=i, channel=CHANNEL_BM25)
             for i, h in enumerate(hits, start=1)], note)


def _points_to_candidates(points, channel: str) -> list[_Candidate]:
    """Qdrant 命中 → 候选(保留顺序即排名,重复的 memory_id 只留第一次)。"""
    out: list[_Candidate] = []
    seen: set[str] = set()
    for point in points:
        memory_id = vector.id_of(point)
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(_Candidate(memory_id=memory_id, rank=len(out) + 1, channel=channel,
                              payload=dict(point.payload or {})))
    return out


def _bm25_candidates(user_id: str, query: str, profile: SearchProfile, *,
                     scope: str) -> tuple[list[_Candidate], str, bool]:
    """降级通道:在该用户当前作用域的有效正文上现算 BM25。

    语料有上限:**超限如实记降级并说明覆盖范围**(连总数一起报出来),不是悄悄只查最近
    几条 —— 「检索质量下降」不能变成用户看不见的事。这条通道只看 MySQL 正文,
    与向量索引建没建好无关。
    """
    from app.memory import db, repo

    cap = max(1, int(get_settings().memory_bm25_corpus_limit))
    try:
        with db.session_scope() as session:
            rows = repo.active_texts(session, user_id, limit=cap + 1, scope=scope)
            total = repo.count_items_in_scope(session, scope=scope, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 —— MySQL 不可用:本路没有候选
        logger.warning("记忆关键词召回不可用(读不到语料):%s:%s", type(exc).__name__, exc)
        return [], "关键词召回不可用(读不到记忆正文),本次只用向量召回", False
    note = ""
    if len(rows) > cap:
        rows = rows[:cap]
        note = f"{DEGRADE_BM25_CORPUS_CAPPED}(覆盖 {cap} / 共 {total} 条)"
    hits = memory_text.bm25_scores(query, rows)
    return ([_Candidate(memory_id=h.doc_id, rank=i, channel=CHANNEL_BM25)
             for i, h in enumerate(hits[: max(1, profile.bm25_k)], start=1)], note, True)


def _current_version() -> str:
    from app.memory import service

    return service.embedding_version()


# ---- MySQL 校验(归属 / 状态 / 作用域 / 与当前事实对齐) ----


def _validate(user_id: str, *, scope: str, candidates: list[_Candidate],
              dense_cap: int, snapshots: dict | None = None) -> tuple[list[tuple[str, int | None, int | None]], dict]:
    """按 memory_id 去重并校验候选;返回 [(memory_id, 向量排名, BM25 排名)] 与过滤计数。

    校验口径**按候选来自哪一路分开**:
    - **dense / sparse(Qdrant 命中)**:归属必须在 MySQL 里对上,而且命中携带的
      revision / content_hash / embedding_version 必须与**当前事实**逐项一致 ——
      Qdrant 里可能残留旧版本的点(改版之前写的、换模型之前写的),那种"语义相似"
      不可信;对不上就丢弃(记 filtered_stale),绝不拿它当证据。
    - **bm25(降级通道,正文直接来自 MySQL)**:天然是当前正文,只看归属 / 状态 / 作用域,
      **不要求向量已建好** —— 索引落后不该让用户搜不到自己刚写下的记忆。

    归属这一项即便召回层已按用户过滤也再查一次:Qdrant 的过滤条件一旦配错就是越权读到
    别人的私人记忆,以 MySQL 为准。
    """
    from app.memory import db, repo

    wanted = list(dict.fromkeys([c.memory_id for c in candidates]))
    counts = {"filtered_missing": 0, "filtered_stale": 0, "filtered_foreign": 0,
              "filtered_scope": 0}
    if not wanted:
        return [], counts
    version = _current_version()
    with db.session_scope() as session:
        rows = repo.list_by_ids(session, wanted)
    by_id = {item.id: item for item in rows}

    def _why(cand: _Candidate) -> str:
        """这条候选为什么不能用?(空串 = 可以用)。"""
        item = by_id.get(cand.memory_id)
        if item is None:
            return "filtered_missing"            # 已删 / 不存在
        if item.user_id != user_id:
            return "filtered_foreign"            # 不该出现;出现就是越权信号,记下来
        if item.scope != scope:
            return "filtered_scope"              # 作用域不符(索引标错 / 串了作用域)
        if cand.channel == CHANNEL_BM25:
            return ""                            # 正文来自 MySQL:不要求向量已建好
        payload = cand.payload or {}
        if str(payload.get("memory_id") or "") != item.id:
            return "filtered_stale"              # payload 自相矛盾:不采信
        if int(payload.get("revision") or 0) != int(item.revision):
            return "filtered_stale"              # 索引还停在旧版本
        if str(payload.get("content_hash") or "") != item.content_hash:
            return "filtered_stale"              # 正文改过而点没重建
        if str(payload.get("embedding_version") or "") != version:
            return "filtered_stale"              # 换模型之前写的向量
        if int(payload.get("generation") or 0) != int(item.generation):
            return "filtered_stale"              # 清除之前写的点(generation 对不上)
        if not vector.payload_matches(payload, item, version):
            return "filtered_stale"
        return ""

    ordered: list[tuple[str, int | None, int | None]] = []
    seen: set[str] = set()
    excluded: dict[str, str] = {}      # memory_id → 排除原因(最终采纳的会被摘掉)
    dense_kept = 0
    for cand in sorted([c for c in candidates if c.channel == CHANNEL_DENSE],
                       key=lambda c: c.rank):
        # 向量侧最后按 dense_cap 收口:调用方多取了一些(oversample)用来补被过滤掉的,
        # 但采纳的条数仍以配置的 vector_k 为准
        if dense_kept >= max(1, dense_cap):
            break
        if cand.memory_id in seen:
            continue
        reason = _why(cand)
        if reason:
            excluded[cand.memory_id] = reason
            continue
        seen.add(cand.memory_id)
        excluded.pop(cand.memory_id, None)
        dense_kept += 1
        ordered.append((cand.memory_id, cand.rank, None))
    for cand in sorted([c for c in candidates if c.channel != CHANNEL_DENSE],
                       key=lambda c: c.rank):
        if cand.memory_id in seen:               # 两路都召回:补上关键词这一路的排名
            for i, row in enumerate(ordered):
                if row[0] == cand.memory_id:
                    ordered[i] = (row[0], row[1], cand.rank)
                    break
            continue
        reason = _why(cand)                      # 两次查询之间被删 / 被改:如实算缺席
        if reason:
            excluded.setdefault(cand.memory_id, reason)
            continue
        seen.add(cand.memory_id)
        excluded.pop(cand.memory_id, None)       # 关键词这一路能用:不算被排除
        ordered.append((cand.memory_id, None, cand.rank))
    # 计数按**记忆条数**算,不按候选次数:同一条记忆同时被两路召回、又同时被排除时,
    # 报「2 条不一致」会让运维以为坏了两个东西(实际只有一个)。
    for reason in excluded.values():
        counts[reason] += 1
    if snapshots is not None:
        snapshots.update({row[0]: by_id[row[0]] for row in ordered})
    return ordered, counts


# ---- 融合与重排 ----


def _rrf(ordered, *, k: int) -> list[tuple[str, float, int | None, int | None]]:
    """RRF:每路各按自己的排名贡献 1/(k+rank),两路相加 —— 不是把分数相加。"""
    fused: list[tuple[str, float, int | None, int | None]] = []
    for memory_id, dense_rank, keyword_rank in ordered:
        score = 0.0
        if dense_rank:
            score += 1.0 / (k + dense_rank)
        if keyword_rank:
            score += 1.0 / (k + keyword_rank)
        fused.append((memory_id, score, dense_rank, keyword_rank))
    fused.sort(key=lambda row: (-row[1], row[0]))
    return fused


def _rerank(user_id: str, query: str, fused, profile: SearchProfile, *, limit: int,
            purpose: str, snapshots: dict | None = None, trace: dict | None = None) -> tuple[list[tuple[str, float, float, int | None, int | None, str]],
                                   str]:
    """重排头部候选;未配置 / 失败 / 输入超限都回退到融合顺序并给出说明。

    返回 (按最终顺序排列的候选, 降级说明)。候选是
    (memory_id, 排序分数, RRF 分数, 向量排名, 关键词排名, 来源)。

    `top_n` 取**本次实际的最终条数**(调用方覆盖 top_k 时就是那个值,不是档位默认值):
    否则会出现「按 50 条重排、最终只留 3 条」的错配 —— 多花的重排预算白费,而且模型是
    在被截掉的候选里排的序。
    """
    head = fused[: max(1, profile.rerank_k)]
    tail = fused[len(head):]

    def fallback(rows) -> list[tuple[str, float, float, int | None, int | None, str]]:
        return [(mid, rrf, rrf, dr, br, "rrf") for mid, rrf, dr, br in rows]

    if not head:
        return [], ""
    if not rerank_client.configured():
        return fallback(fused), rerank_client.config_note()

    from app.memory import db, repo

    if snapshots is None:
        with db.session_scope() as session:
            items = {item.id: item for item in repo.list_by_ids(session, [row[0] for row in head])}
    else:
        items = snapshots
    docs: list[str] = []
    kept: list[tuple[str, float, int | None, int | None]] = []
    note = ""
    max_docs = max(1, int(get_settings().memory_rerank_max_docs))
    max_chars = max(200, int(get_settings().memory_rerank_max_chars))
    budget = 0
    for row in head:
        item = items.get(row[0])
        if item is None:                            # 刚被删:不当候选,也不重排
            continue
        if len(kept) >= max_docs or (kept and budget + len(item.text) > max_chars):
            note = DEGRADE_RERANK_INPUT_CAPPED
            break
        budget += len(item.text)
        kept.append(row)
        docs.append(item.text)
    tail = [*[row for row in head if row not in kept], *tail]
    if trace is not None:
        trace["rerank_input"] = [row[0] for row in kept]
    if not docs:
        return fallback(fused), note

    try:
        with bind_context(purpose=purpose):
            outcome = rerank_client.rerank(query, docs, top_n=min(max(1, int(limit)), len(docs)),
                                           instruct=profile.rerank_instruct)
    except Exception as exc:  # noqa: BLE001 —— 客户端只对编程错误抛;这里也兜住,绝不影响检索
        logger.warning("重排序异常已回退:%s:%s", type(exc).__name__, exc)
        outcome = rerank_client.RerankOutcome(error=f"{type(exc).__name__}:{exc}")

    if not outcome.ok:
        return fallback(fused), f"重排序失败,已回退到 RRF 融合结果:{outcome.error}"

    if trace is not None:
        trace["rerank_returned"] = [{"memory_id": kept[i][0], "rank": rank, "score": outcome.scores[i]}
                                    for rank, i in enumerate(outcome.order, 1)]

    ranked: list[tuple[str, float, float, int | None, int | None, str]] = []
    kept_ids: set[str] = set()
    for index in outcome.order:                     # 保持模型给的顺序,不再按 RRF 覆盖
        mid, rrf, dr, br = kept[index]
        ranked.append((mid, outcome.scores[index], rrf, dr, br, "rerank"))
        kept_ids.add(mid)
    for mid, rrf, dr, br in [*kept, *tail]:         # 没进 top_n 的按融合顺序跟在后面
        if mid not in kept_ids:
            ranked.append((mid, rrf, rrf, dr, br, "rrf"))
    if outcome.ignored:
        note = f"{note};" if note else ""
        note += f"重排序返回里另有 {outcome.ignored} 条非法条目被忽略"
    # 重排成功也要把「输入被截断」带回去:截断悄悄发生就等于骗调用方说全排过了
    return ranked, note


# ---- 最终结果与上下文预算 ----


def _finalize(ranked, *, limit: int, budget: int, snapshots: dict) -> tuple[list[Evidence], str]:
    """取最终 top_k,并按上下文预算截断正文(截断就要标出来,不伪称完整)。"""
    from app.memory import db, repo

    head = ranked[: max(1, limit)]
    if not head:
        return [], ""
    with db.session_scope() as session:
        items = {item.id: item for item in repo.list_by_ids(session, [r[0] for r in head])}
    hits: list[Evidence] = []
    used = 0
    trimmed = False
    changed = False
    for mid, score, rrf, dense_rank, keyword_rank, origin in head:
        item = items.get(mid)
        if item is None:
            changed = True
            continue
        expected = snapshots.get(mid)
        if expected is None or any(getattr(item, key) != getattr(expected, key) for key in (
                "user_id", "scope", "revision", "meta_version", "content_hash", "generation")):
            changed = True
            continue
        text = item.text
        room = budget - used
        if room <= 0:
            trimmed = True
            break
        cut = False
        if len(text) > room:
            text = text[:room]
            cut = True
            trimmed = True
        metadata = context_label(item.fact_context)
        metadata_room = max(0, room - len(text))
        if len(metadata) > metadata_room:
            metadata = metadata[:metadata_room]
            cut = trimmed = True
        used += len(text) + len(metadata)
        hits.append(Evidence(memory_id=mid, text=text, score=float(score), origin=origin,
                             rrf=float(rrf), vector_rank=dense_rank, bm25_rank=keyword_rank,
                             updated_at=item.updated_at, thread_id=item.thread_id,
                             revision=item.revision, trimmed=cut, kind=item.kind,
                             event_time=item.event_time, fact_context=item.fact_context, context_text=metadata, meta_version=item.meta_version))
    notes = [DEGRADE_BUDGET] if trimmed else []
    if changed:
        notes.append("检索期间记忆已变化，已丢弃版本不一致的候选")
    return hits, ";".join(notes)


from app.memory.temporal import context_label


def format_evidence(result: SearchResult) -> str:
    """把证据拼成给 LLM 的参考块(明确标注是参考数据,不是指令,也不能当权限)。"""
    if not result.hits:
        return ""
    blocks = []
    for i, hit in enumerate(result.hits, start=1):
        suffix = "(截断)" if hit.trimmed else ""
        stamp = hit.updated_at.strftime("%Y-%m-%d") if hit.updated_at else "时间未知"
        event = f";发生时间 {hit.event_time.strftime('%Y-%m-%d')}" if hit.event_time else ""
        blocks.append(f"[记忆 {i}｜记录更新 {stamp}{event}{suffix}] {hit.text}"
                      + hit.context_text)
    return "\n".join(blocks)
