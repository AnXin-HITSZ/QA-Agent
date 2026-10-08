"""「我的记忆」接口的请求 / 响应模型（§12）。

只管数据形状：权限在路由守卫上（require_user），归属在 service 层按 Principal 的
user_id 过滤 —— 这里的模型**不接受**任何 user_id 字段，前端传不了别人的归属。

几个字段口径：
- `index_state`：这条记忆的向量是否与当前 Embedding 版本同步（synced / pending）。
  索引是派生数据，pending 不代表记忆丢了 —— 后台会补，界面照实显示，别让用户以为没保存；
- `enabled` / `write_enabled` / `search_enabled`：三个开关的当前状态。**任何一个为 false
  都只是暂停，不是删除**（文案里要写清，见 docs/长期记忆系统技术方案.md「删除语义」）；
- `degraded` / `error`：检索或索引这次没做到最好时的**如实说明**（不静默降级）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# 请求体上限：正文超过配置的 memory_max_text_chars 由服务层截断（配置是运维口径），
# 这里只拦「明显不是一句记忆」的超大请求体，避免把 body 当传送带。
MAX_REQUEST_TEXT_CHARS = 4000
MAX_QUERY_CHARS = 200


class MemoryItemView(BaseModel):
    """一条记忆（正文 + 必要的来源与状态；不含 user_id —— 都是调用者自己的）。"""

    id: str = Field(..., description="记忆 id")
    text: str = Field(..., description="记忆正文（一句事实）")
    status: str = Field(..., description="active / deleted（列表默认只返回 active）")
    origin: str = Field(..., description="这条记忆**最初**怎么来的：llm=会话提取，user=用户手添")
    revision: int = Field(..., description="版本号：每被改写一次 +1（编辑时的并发保护依据）")
    thread_id: str | None = Field(default=None, description="来源会话 id（用户手添的为 null）")
    created_at: datetime = Field(..., description="入库时间（UTC）")
    updated_at: datetime = Field(..., description="最后变更时间（UTC）")
    index_state: str = Field(..., description="synced=向量已同步到当前 Embedding 版本；pending=待补索引")
    indexed_at: datetime | None = Field(default=None, description="最近一次写入索引的时间；未索引为 null")


class MemoryListResponse(BaseModel):
    """列表 / 检索结果。带 query 时走检索（结果按相关性排序），否则按更新时间倒序分页。"""

    enabled: bool = Field(..., description="记忆功能是否可用（未配 MySQL 时 false）")
    items: list[MemoryItemView] = Field(default_factory=list, description="这一页的记忆")
    total: int = Field(default=0, description="有效记忆总数（检索模式下 = 本次返回条数）")
    index_pending: int = Field(default=0, description="待补索引的条数（后台会自愈，不影响记忆本身）")
    query: str = Field(default="", description="本次的检索词（空 = 普通列表）")
    degraded: list[str] = Field(default_factory=list, description="本次没做到最好的地方（如实说明）")
    error: str = Field(default="", description="检索整体不可用时的原因；空 = 正常")


class MemoryAddRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_REQUEST_TEXT_CHARS,
                      description="要记住的内容（一句事实；过长会被截断到系统上限）")


class MemoryEditRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_REQUEST_TEXT_CHARS,
                      description="改写后的内容（版本在读取与写入之间变过则不覆盖，返回 409）")


class MemoryHistoryItem(BaseModel):
    """一条变更审计（正文在「彻底清除」时已脱敏为 null）。"""

    id: int = Field(..., description="审计行 id（自增，越大越新）")
    memory_id: str = Field(..., description="被改动的记忆 id")
    event: str = Field(..., description="ADD / UPDATE / DELETE")
    old_text: str | None = Field(default=None, description="改动前的正文；ADD 与已脱敏的行为 null")
    new_text: str | None = Field(default=None, description="改动后的正文；DELETE 与已脱敏的行为 null")
    actor: str = Field(..., description="llm=会话提取 / 维护，user=用户操作，system=系统")
    reason: str = Field(default="", description="这次变更的说明")
    created_at: datetime = Field(..., description="发生时间（UTC）")


class MemoryHistoryResponse(BaseModel):
    enabled: bool = Field(..., description="记忆功能是否可用")
    items: list[MemoryHistoryItem] = Field(default_factory=list, description="最近的变更（新的在前）")


class MemoryClearResponse(BaseModel):
    """「彻底删除我的长期记忆」的真实结果（逐项如实汇报，不粉饰）。

    注意：这不等于删除账号与聊天记录 —— 对话仍在，后续新交流还会重新提取出同样的事实。
    """

    items: int = Field(..., description="删掉的记忆条数")
    jobs: int = Field(..., description="删掉的（含对话正文的）待执行任务数")
    history: int = Field(..., description="正文被脱敏的审计行数（行本身保留）")
    generation: int = Field(..., description="该用户的新记忆代次：之前入队的任务据此全部作废")
    vectors: bool = Field(..., description="索引是否已清干净；false 表示残留（事实已删，可稍后重建）")
    points: int = Field(default=0, description="清掉的向量点数（vector=true 时才有意义）")
    cleanup: str = Field(default="done",
                         description="done=索引已清干净；pending=已登记台账，后台按退避重试")
    degraded: list[str] = Field(default_factory=list, description="没做到最好的地方")


class MemoryDeleteResponse(BaseModel):
    """「删除一条记忆」的结果：事实已删之外，索引侧是**清干净了还是待清理**。

    删除只有在事实层才算数（触发它的那个答案不会再被检索到）；向量清理失败不会回滚删除，
    但也不能瞒着 —— `cleanup=pending` 表示后台会重试，界面照实显示「正在清理」。
    """

    id: str = Field(..., description="被删除的记忆 id")
    cleanup: str = Field(..., description="done=索引已清干净；pending=已登记台账，后台按退避重试")
    degraded: list[str] = Field(default_factory=list, description="没做到最好的地方")


class MemoryReindexResponse(BaseModel):
    """把「待索引」的记忆补写进 Qdrant 的结果。"""

    requested: int = Field(default=0, description="本次取出的待索引条数")
    indexed: int = Field(default=0, description="成功写入索引的条数（重新算了向量）")
    payload_only: int = Field(default=0,
                              description="正文没变、只刷新了元数据的条数（这些**没有**重新向量化）")
    deferred: int = Field(default=0, description="仍未能写入的条数（留待下一轮）")
    error: str = Field(default="", description="失败原因（脱敏摘要）")


class MemoryJobCounts(BaseModel):
    pending: int = Field(default=0, description="待执行（含退避等待）")
    running: int = Field(default=0, description="执行中")
    succeeded: int = Field(default=0, description="已成功（终态）")
    failed: int = Field(default=0, description="已失败（终态）")


class MemoryStatusResponse(BaseModel):
    """「我的记忆」页顶部的功能状态（§12：显示功能开关与保存 / 索引状态）。

    三组信息各自的含义：开关决定**行为**（都只是暂停，不删数据）；items 是当前规模；
    jobs / last_error 说明后台提取与索引有没有卡住。
    """

    enabled: bool = Field(..., description="记忆功能是否可用（未配 MySQL 时 false）")
    write_enabled: bool = Field(..., description="自动写入开关：false=暂停新的提取（已存记忆与手动增删改照常）")
    search_enabled: bool = Field(..., description="检索开关：false=回答时不再注入记忆（「我的记忆」页里的检索照常）")
    maintenance_enabled: bool = Field(..., description="维护决策开关：false=只新增不改写（显式降级，不是关闭记忆）")
    configured: bool = Field(..., description="是否配了 MySQL")
    items: int = Field(default=0, description="有效记忆条数")
    deleted_items: int = Field(default=0, description="已软删的记忆条数（保留供追溯）")
    index_pending: int = Field(default=0, description="待补索引条数")
    cleanup_pending: int = Field(default=0,
                                 description="删除/清除留下的向量清理台账里待重试的条数")
    cleanup_failed: int = Field(default=0,
                                description="清理台账里重试用尽的条数（界面显示「清理失败」）")
    sparse_search: bool = Field(default=False,
                                description="集合里是否真有稀疏通道；false=关键词召回走降级通道")
    jobs: MemoryJobCounts = Field(default_factory=MemoryJobCounts, description="本用户的后台任务计数")
    worker_running: bool = Field(default=False, description="后台提取线程是否在跑")
    last_error: str = Field(default="", description="后台任务最近一次失败的原因（脱敏摘要）")
    last_run_at: str = Field(default="", description="后台最近一轮完成时间（ISO8601；从未跑过为空）")
