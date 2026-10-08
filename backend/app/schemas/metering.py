"""调用日志与费用统计接口的响应模型(见 docs/调用日志与费用统计技术方案.md §7)。

口径说明(前端文案必须与此一致):
- 一条 call = 一次真实外部请求(或一次逻辑调用,http_attempts 记它背后的请求次数);
- 金额一律是「按配置单价 × 用量」的**估算费用**,不是官方账单,字段名统一带 estimated
  或配 cost_status 标注;缺用量 / 缺价格时金额为空 + 原因写在 note 里,绝不用 0 冒充;
- 缓存命中与实际调用分列,不合并;不同币种 / 不同单位分开返回,不给跨币相加的合计。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.metering.model import SERVICE_EMBEDDING, SERVICE_LLM, SERVICE_OCR, SERVICE_RERANK
from app.metering.pricing import UNITS

# 价目 / 日志共用的服务清单(与 model 里的常量同源)。
# llm / rerank 是长期记忆引入的:记忆提取与维护决策的聊天模型、记忆检索的重排序。
SERVICES = (SERVICE_EMBEDDING, SERVICE_OCR, SERVICE_LLM, SERVICE_RERANK)


class CallItemView(BaseModel):
    """一次请求归属到某个文件 / 页面的估算分摊(批量请求覆盖多文件时有多行)。"""

    document_id: str | None = Field(default=None, description="文件身份(登记表里的 id)")
    oss_key: str | None = Field(default=None, description="知识库相对 key")
    page_no: int | None = Field(default=None, description="页码(按页调用时)")
    text_count: int = Field(default=0, description="该文件在这次请求里占的文本条数(分摊权重)")
    allocated_cost: str | None = Field(default=None, description="分摊到该文件的**估算**金额;无可分摊金额时为空")
    allocation_note: str = Field(default="", description="分摊口径说明(标注为估算,非账单金额)")


class CallLogItem(BaseModel):
    """调用日志一行(列表与详情共用;详情多带 items)。"""

    event_id: str = Field(..., description="事件 id(唯一,补写重放幂等靠它)")
    occurred_at: str = Field(..., description="请求开始时间(UTC,ISO8601;前端按本地时区显示)")
    service: str = Field(..., description="embedding / ocr / llm / rerank")
    purpose: str = Field(..., description=("document_index(索引)/ query(检索)/"
                                           " memory_extract / memory_maintenance / memory_search"
                                           "(长期记忆的提取 / 维护 / 检索)"))
    provider: str = Field(..., description="供应商(端点主机名 / aliyun)")
    target: str = Field(default="", description="模型名 / 供应商 OCR Type")
    endpoint: str = Field(default="", description="脱敏后的端点(只有主机名,不含密钥与签名参数)")
    call_group: str | None = Field(default=None, description="同一逻辑调用的多次尝试共享")
    attempt_no: int = Field(default=1, description="本行是第几次尝试(我们自己的重试循环逐次记行)")
    retry_of: str | None = Field(default=None, description="上一次尝试的事件 id(重试链路)")
    http_attempts: int = Field(default=1, description="本行背后的真实 HTTP 请求次数(SDK 内部重试计入)")
    duration_ms: int = Field(default=0, description="本次请求耗时(毫秒)")
    status: str = Field(..., description="success / failure(业务调用是否成功,不是日志写入状态)")
    error_class: str = Field(default="", description="异常类名(已脱敏)")
    error_message: str = Field(default="", description="异常摘要(已脱敏,截断)")
    http_status: int | None = Field(default=None, description="HTTP 状态码")
    provider_request_id: str | None = Field(default=None, description="供应商请求 id(对账用)")
    usage_quantity: str | None = Field(default=None, description="用量(字符串十进制);为空 = 拿不到用量")
    usage_unit: str = Field(default="", description="token / request / page")
    usage_source: str = Field(default="unknown", description="vendor_response(供应商报的)/ local_count(本端计数)/ unknown")
    usage_note: str = Field(default="", description="用量的来源与缺口说明")
    billing_quantity: str | None = Field(default=None, description="计费数量(1k_tokens 会除以 1000)")
    billing_unit: str = Field(default="", description="计费单位:1k_tokens / request / page")
    billing_status: str = Field(default="unknown", description="billable(供应商侧应计费)/ unknown(是否计费未知)")
    billing_note: str = Field(default="", description="计费判断依据")
    cost_amount: str | None = Field(default=None, description="**估算**费用(字符串十进制);为空 = 无法估算")
    currency: str | None = Field(default=None, description="币种(CNY 等);不同币种不合并")
    cost_status: str = Field(default="unknown", description="estimated(估算)/ unknown(无法估算)")
    cost_note: str = Field(default="", description="估算依据 / 无法估算的原因")
    price_id: int | None = Field(default=None, description="当时生效的价目 id")
    price_version: str | None = Field(default=None, description="价目内容摘要(改价后可核对历史用的是哪版)")
    price_snapshot: dict | None = Field(default=None, description="事件发生时的价目快照(不可变,改价不重算历史)")
    job_id: str | None = Field(default=None, description="索引任务 id")
    document_id: str | None = Field(default=None, description="文件身份")
    oss_key: str | None = Field(default=None, description="知识库相对 key(前端据此跳转文件)")
    page_no: int | None = Field(default=None, description="页码")
    items: list[CallItemView] | None = Field(default=None, description="按文件的估算分摊(详情接口返回)")


class CallLogPage(BaseModel):
    """分页调用日志(只返回当前页;不做全量导出)。"""

    total: int = Field(..., description="符合条件的总条数")
    offset: int = Field(..., description="本页偏移")
    limit: int = Field(..., description="本页上限")
    items: list[CallLogItem] = Field(default_factory=list, description="调用记录(按开始时间倒序)")


class MeteringTotals(BaseModel):
    """总量:实际调用与失败分开,未知用量 / 未知费用单独计数。"""

    calls: int = Field(default=0, description="调用记录条数(逻辑调用 / 逐次尝试)")
    success: int = Field(default=0, description="成功条数")
    failure: int = Field(default=0, description="失败条数")
    unknown_usage: int = Field(default=0, description="未取得用量的条数")
    unknown_cost: int = Field(default=0, description="无法估算费用的条数(缺用量或缺价格)")
    billing_unknown: int = Field(default=0, description="供应商侧是否计费未知的条数")
    http_attempts: int = Field(default=0, description="真实发出的 HTTP 请求次数(≥ 记录条数)")


class ServiceStat(BaseModel):
    service: str = Field(..., description="embedding / ocr / llm / rerank")
    calls: int = Field(default=0, description="记录条数")
    success: int = Field(default=0, description="成功条数")
    failure: int = Field(default=0, description="失败条数")
    unknown_usage: int = Field(default=0, description="未取得用量的条数")
    unknown_cost: int = Field(default=0, description="无法估算费用的条数")
    http_attempts: int = Field(default=0, description="真实请求次数")


class DayStat(BaseModel):
    day: str = Field(..., description="UTC 日期(YYYY-MM-DD)")
    service: str = Field(..., description="服务")
    calls: int = Field(default=0, description="记录条数")
    failure: int = Field(default=0, description="失败条数")


class CostStat(BaseModel):
    """按服务 + 币种分开的**估算**费用(不同币种不合并)。"""

    service: str = Field(..., description="服务")
    currency: str = Field(..., description="币种")
    events: int = Field(default=0, description="有金额的条数")
    amount: str = Field(default="0", description="估算金额合计(字符串十进制,非官方账单)")


class UsageStat(BaseModel):
    """按服务 + 单位分开的用量(不同单位不合并)。"""

    service: str = Field(..., description="服务")
    unit: str = Field(..., description="token / request / page")
    quantity: str = Field(default="0", description="用量合计")


class CacheStat(BaseModel):
    """缓存命中统计:命中 / 未命中 / 等待复用 / 去重跳过分开记。"""

    layer: str = Field(..., description="ocr_raw / ocr_text / embedding")
    unit: str = Field(default="", description="page / text")
    hit: int = Field(default=0, description="直接读到缓存(省下一次外部调用或一次本地转换)")
    miss: int = Field(default=0, description="未命中、需要计算(embedding 的 miss 才会产生费用)")
    shared: int = Field(default=0, description="等锁 / 双检期间复用了他人刚算出的结果(无本次外部调用)")
    skipped: int = Field(default=0, description="同批重复项,去重后不单独计算")


class PersistenceHealth(BaseModel):
    """日志持久化自身健康:补写失败 / 队列积压都在这里如实暴露。"""

    enabled: bool = Field(default=False, description="是否启用计量")
    configured: bool = Field(default=False, description="是否配置了 MySQL 连接")
    running: bool = Field(default=False, description="后台补写线程是否在跑")
    db_ok: bool | None = Field(default=None, description="最近一次数据库探测结果;null = 尚未探测")
    db_error: str = Field(default="", description="最近一次数据库错误(已脱敏)")
    queued: int = Field(default=0, description="队列中待写入的条数")
    pending: int = Field(default=0, description="补写目录里待写入的条数")
    claimed: int = Field(default=0, description="补写目录里正在被某个进程写入的条数")
    pending_dir: str = Field(default="", description="补写目录(故障恢复用,不是可查询的日志库)")
    flushed: int = Field(default=0, description="本进程已写入条数")
    backfilled: int = Field(default=0, description="本进程补写成功条数")
    spilled: int = Field(default=0, description="转入补写目录的条数")
    lost: int = Field(default=0, description="连补写文件都写失败的条数(>0 表示确有丢失,需人工关注)")
    last_flush_at: str | None = Field(default=None, description="最近一次成功写入时间(UTC)")
    last_error: str = Field(default="", description="最近一次写入错误")
    price_rules: int = Field(default=0, description="已载入内存的价目条数")
    price_error: str = Field(default="", description="价格表载入错误")
    message: str = Field(default="", description="未启用时的说明")


class MeteringSummary(BaseModel):
    """概览:总量 + 分组 + 缓存 + 持久化健康(供前端首屏)。"""

    since: str | None = Field(default=None, description="时间窗起(UTC;含)")
    until: str | None = Field(default=None, description="时间窗止(UTC;不含)")
    filters: dict = Field(default_factory=dict, description="本次生效的过滤条件")
    error: str = Field(default="", description="统计失败的原因(非空时下面的数字不可信,不是「没有调用」)")
    totals: MeteringTotals = Field(default_factory=MeteringTotals, description="总量")
    by_service: list[ServiceStat] = Field(default_factory=list, description="按服务分组")
    by_day: list[DayStat] = Field(default_factory=list, description="按天分组(UTC 日期)")
    cost_by_service_currency: list[CostStat] = Field(
        default_factory=list, description="按服务 + 币种的估算费用(非账单)")
    usage_by_service_unit: list[UsageStat] = Field(
        default_factory=list, description="按服务 + 单位的用量")
    cache: list[CacheStat] = Field(default_factory=list, description="缓存命中统计")
    persistence: PersistenceHealth = Field(default_factory=PersistenceHealth,
                                           description="日志持久化健康")


class PriceRuleView(BaseModel):
    """一条价目(供前端解释「估算」的依据;在界面「估算依据」里维护,也只增 / 删)。"""

    id: int | None = Field(default=None, description="价目 id")
    service: str = Field(..., description="embedding / ocr / llm / rerank")
    provider: str = Field(..., description="供应商")
    target: str = Field(default="", description="模型 / OCR Type;空 = 该服务通用价")
    unit: str = Field(..., description="计费单位:1k_tokens / request / page")
    currency: str = Field(..., description="币种")
    unit_price: str = Field(..., description="单价(字符串十进制)")
    effective_from: str = Field(..., description="生效时间(UTC)")
    source: str = Field(default="", description="价格来源(官方价格页地址 / 核实日期)")
    note: str = Field(default="", description="备注")


class PriceRuleInput(BaseModel):
    """新增价目的请求体(只增语义;约束对齐 price_config 列定义与「必须引用官方来源」的口径)。"""

    service: str = Field(..., description="embedding / ocr / llm / rerank")
    provider: str = Field(..., max_length=32, description="供应商(与调用日志里的 provider 一致)")
    target: str = Field(default="", max_length=64, description="模型名 / OCR Type;空 = 该服务通用价")
    unit: str = Field(..., description="计费单位:1k_tokens / request / page")
    currency: str = Field(default="CNY", description="币种(3 位大写字母,如 CNY / USD)")
    unit_price: Decimal = Field(..., description="单价(每计费单位;最多 8 位小数,非负)")
    effective_from: str = Field(..., min_length=1,
                                description="生效时间(ISO8601 带时区,或 epoch 秒;缺省按 UTC)")
    source: str = Field(..., max_length=255,
                        description="价格来源:官方价格页地址 / 核实日期(必填,便于对账)")
    note: str = Field(default="", max_length=255, description="备注")

    @field_validator("service")
    @classmethod
    def _known_service(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in SERVICES:
            raise ValueError(f"未知服务 {v!r}(可选:{' / '.join(SERVICES)})")
        return v

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in UNITS:
            raise ValueError(f"未知计费单位 {v!r}(可选:{' / '.join(UNITS)})")
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if len(v) != 3 or not v.isascii() or not v.isalpha():
            raise ValueError("币种必须是 3 位字母代码(如 CNY / USD)")
        return v

    @field_validator("provider", "source")
    @classmethod
    def _required_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("不能为空")
        return v

    @field_validator("target", "note", "effective_from")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("unit_price")
    @classmethod
    def _price(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("单价必须是有限数")
        if v < 0:
            raise ValueError("单价不能为负数")
        exponent = v.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -8:
            raise ValueError("单价最多 8 位小数(列定义 DECIMAL(18,8))")
        if v >= Decimal("10000000000"):          # DECIMAL(18,8) 整数位上限 10 位
            raise ValueError("单价超出 DECIMAL(18,8) 可存范围")
        return v


class PriceRuleList(BaseModel):
    items: list[PriceRuleView] = Field(default_factory=list, description="当前价目表(按服务/供应商/单位/生效时间)")
