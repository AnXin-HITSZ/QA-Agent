"""记忆维护决策(§6 自动维护):新事实 vs 已有候选 → ADD / UPDATE / DELETE / NONE。

与 Mem0（v2.2.1，Apache-2.0）的对应关系:对应其 `_update_memory` 那一层 ——
「拿新事实去问模型,该加、该改,还是该删」。三处刻意的差异:

1. **按操作建模**:不像 Mem0 那样靠一个宽松 schema + 提示词自律,这里 ADD / UPDATE /
   DELETE / NONE 各有自己的 Pydantic 模型(extra=forbid),缺字段 / 多字段 / 用错形态
   都判为不合法并记原因 —— 「模型说了 UPDATE 却没给 id」这类错误不会滑到落库那一步。
2. **只能动授权候选**:决策里出现的 id 必须在**本次检索返回给它的候选**之内,
   且同一个 id 最多被一个事件动。模型编造 id、或引用候选之外的记忆,一律丢弃并计数
   (落库时 service 还会再按 user_id + revision 复核一次 —— 两层都要)。
3. **不做「合并」的自由发挥**:UPDATE 只接受合并后的**完整正文**,且必须与旧正文有实质
   差异(NFKC 归一后不同),否则按 NONE 处理 —— 避免一次复制粘贴把记忆改成同样的内容。

一条新事实允许产出多个事件(最典型的是「旧说法被推翻」= DELETE 旧 + ADD 新)。
但事件总数有硬上限(默认 5):**超出上限整条决策不采纳**,不做「前 5 个照做」的部分执行。

**全有或全无**:**任何**一个事件不合法 / 未授权 / 重复 / 越限 / 语义矛盾,整条决策都不执行
(抛 MemoryDecisionRejected;协议层面的错先走带纠正提示的有限重试)。理由很实际:
这些事件改的是真实记忆,DELETE + ADD 只做成前半截等于永久丢掉用户的内容;
「能做的先做掉」看起来温和,实际上是让一次非法输出留下一半后果。合法但重复的 NONE
会被合并(那是规范化,不是丢事件);`{"events": []}` 是合法的显式空决策(NOOP),
而 `{}`(没有 events 字段)是格式错误 —— 这两者绝不能混为一谈。

这一层不做任何数据库 / 网络副作用(只有可选的 llm 参数),于是授权与解析规则可以在
单元测试里直接钉死。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from app.config import get_settings
from app.memory.errors import MemoryDecisionRejected, MemoryExtractionError
from app.memory.extract import (
    MAX_FACT_CHARS, FactKind, brief, content_of, looks_secret, strip_fence,
)
from app.memory.models import (
    EVENT_ADD, EVENT_DELETE, EVENT_UPDATE, clamp_text, normalize_text, parse_event_time,
)

from app.memory.temporal import context_label

logger = logging.getLogger(__name__)

# 事件名沿用审计事件常量(ADD/UPDATE/DELETE);NONE 不是审计事件,只表示「什么都不做」
ACTION_NONE = "NONE"

# 决策协议版本:提示词 / 事件结构一改就要 +1。存下来的决策在复用前要比对它 ——
# 拿着旧协议的输出直接执行,等于绕过了新协议的校验。
PROTOCOL_VERSION = "mem-maintain/2"

# 候选清单在提示词里的单条截断上限:候选正文本来就有 MEMORY_MAX_TEXT_CHARS 上限,
# 这里再收一道,防止候选一多就把上下文撑爆。
CANDIDATE_CHARS = 300
CONTEXT_LIMIT = 5000          # 候选清单的字符预算,超出的候选不送进提示词
REASON_CHARS = 200            # 入库的说明长度;MAX_REASON_CHARS 只是防爆上界,超出截断
MAX_REASON_CHARS = 2000

SYSTEM_PROMPT = """你是「长期记忆维护器」。给定一条**新提取的事实**和若干条**已有记忆候选**,决定记忆库要怎么变。

只输出一个 JSON 对象,结构固定为:
{"events": [...], "reason": "一句话说明判断依据"}

events 里每一项必须是下面四种之一:
- 新增:{"event": "ADD", "text": "要写入的新记忆(第三人称,一句话)", "kind": "preference|profile|task|event"}
- 改写:{"event": "UPDATE", "id": "候选里的 id", "text": "合并后的完整正文"}
- 删除:{"event": "DELETE", "id": "候选里的 id"}
- 不变:{"event": "NONE"}

必须遵守:
1. id 只能取候选清单里出现过的 id,**绝不允许自己编造或猜测 id**;候选里没有的,一律不要引用。
2. 新事实与某条候选表达同一件事、且没有任何变化 → 只输出 {"event": "NONE"}。
3. 新事实是对某条候选的补充或细化 → 用 UPDATE,text 给**合并后的完整正文**(不是差异、不是只写新增部分)。
4. 新事实与某条候选矛盾(旧说法已经不成立)→ 先 DELETE 那条,再 ADD 新事实,两个事件都要给。
5. 新事实与所有候选都无关(全新的信息)→ ADD。
6. events 必须给出:NONE 就写 {"events": [{"event": "NONE"}]};没有任何改动时不要省略 events。
7. 每个 id 最多出现在一个事件里;事件总数不超过 5 —— **任何一个事件不合法,整份决策都会被拒绝并重来**,
   所以拿不准就只输出 NONE:宁可不动,也不要错改用户的记忆。
8. 不要输出密码、验证码、API Key、访问密钥、银行卡号等任何凭证;这种内容一律不写入。
9. 当前属性变更可以更新当前属性；历史事件、过去的计划与后来的完成事实分别保留。
10. 只合并同一事实的补充；不同日期或不同事件不可并成摘要。进展不是对旧事实的否定。
11. UPDATE/DELETE 历史事件或计划必须是用户明确纠正旧表述，填写 correction=true；
    不能因计划完成或更晚的记录而删除/改写过去。缺乏纠正证据时 ADD 新事实。
12. 保留来源语言、人物姓名、数值、条件、否定、时间和不确定性。
13. 只输出 JSON,不要解释、不要前后缀、不要 Markdown 代码块。"""

USER_TEMPLATE = """已有记忆候选(每行形如「id | 正文」;没有候选则写「(无)」):
{candidates}

这次新提取的事实:
{fact}

请输出 JSON。"""

_CORRECTIVE = ("上一次输出无法按要求解析({reason})。"
               "请只输出一个合法 JSON 对象,不要任何解释或代码块,结构为 "
               '{{"events": [{{"event": "NONE"}}], "reason": "..."}}。')


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AddEvent(_Base):
    """新增一条记忆(候选里没有可对应的旧记忆)。

    `kind` / `event_time` 与提取协议同名同义(可省略):落库时写进记忆行,
    让「这条是什么类型、这件事什么时候发生的」跟着事实一起留下。
    """

    event: Literal["ADD"]
    text: str = Field(min_length=2, max_length=MAX_FACT_CHARS)
    kind: FactKind | None = None
    event_time: datetime | None = None

    @field_validator("event_time", mode="before")
    @classmethod
    def _parse_time(cls, v):
        return parse_event_time(v)


class UpdateEvent(_Base):
    """改写一条已有记忆(必须给 id 与合并后的完整正文)。"""

    event: Literal["UPDATE"]
    id: str = Field(min_length=4, max_length=64)
    correction: bool = Field(default=False, strict=True)
    text: str = Field(min_length=2, max_length=MAX_FACT_CHARS)
    kind: FactKind | None = None
    event_time: datetime | None = None

    @field_validator("event_time", mode="before")
    @classmethod
    def _parse_time(cls, v):
        return parse_event_time(v)


class DeleteEvent(_Base):
    """删除一条已有记忆(通常与 ADD 配对:旧说法被推翻)。"""

    event: Literal["DELETE"]
    id: str = Field(min_length=4, max_length=64)
    correction: bool = Field(default=False, strict=True)


class NoneEvent(_Base):
    """什么都不做:已有记忆已经准确表达了这条事实(NONE 的含义全流程一致)。"""

    event: Literal["NONE"]


MemoryEvent = Annotated[AddEvent | UpdateEvent | DeleteEvent | NoneEvent,
                        Field(discriminator="event")]


class MaintenanceEnvelope(BaseModel):
    """模型输出的外层结构:{"events": [...], "reason": "..."}。

    reason 是**说明性**字段(一句话依据),所以只做宽松的防爆上界再截断入库:模型啰嗦几句
    不该把整份决策判成结构错误 —— 那等于因为一句注释丢掉已经判定好的事件。events 才是
    协议本体,它的逐项校验一点不放松(见 _EVENT_ADAPTER)。

    `events` **必须显式给出**(没有默认值):`{}` 与 `{"events": []}` 不是一回事 ——
    前者是「没按协议输出」(重试 / 明确失败),后者是「明确地什么都不用做」。
    给默认空列表会让格式错误静默变成「不用维护」。
    """

    model_config = ConfigDict(extra="forbid")

    events: list[Any]
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)


_EVENT_ADAPTER = TypeAdapter(MemoryEvent)


@dataclass
class MaintenanceDecision:
    """一次维护决策:合法事件 + 被丢弃的事件(只记原因)+ 实际调用次数。"""

    events: list[AddEvent | UpdateEvent | DeleteEvent | NoneEvent] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    attempts: int = 0
    reason: str = ""      # 模型给的说明(只随结果入库,绝不写日志 —— 可能夹带正文)

    @property
    def changes(self) -> list:
        """真正要落库的事件(NONE 只是「不用动」的显式回答)。"""
        return [e for e in self.events if not isinstance(e, NoneEvent)]

    @property
    def unchanged(self) -> bool:
        return not self.changes


@dataclass(frozen=True)
class Candidate:
    """一条授权候选:来自本次检索,带它当时的版本号(落库时按这个版本做 CAS)。"""

    memory_id: str
    text: str
    revision: int
    kind: str = ""
    fact_context: dict = field(default_factory=dict)
    meta_version: int = 0


# ---- 提示词 ----


def visible_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """**真正会展示给模型**的那批候选(按字符预算裁掉装不下的)。

    授权集合必须取自这里,而不是「传进来的全部候选」:被预算裁掉的候选模型根本没见过,
    它当然不可能引用;若授权时仍认这些 id,就等于承认模型可以引用它没看到的东西 ——
    「引用只能来自真正展示过的候选」这条保证要落在同一个函数上,不能两处各算一遍。
    """
    kept: list[Candidate] = []
    used = 0
    for cand in candidates or ():
        line = f"{cand.memory_id} | {cand.kind} | {clamp_text(cand.text, CANDIDATE_CHARS)}" + context_label(cand.fact_context)
        if used + len(line) > CONTEXT_LIMIT:
            break                           # 预算用完就不再塞:少给候选好过超上下文
        used += len(line)
        kept.append(cand)
    return kept


def build_messages(fact: str, candidates: Sequence[Candidate]) -> list:
    """拼维护决策的提示词(候选按传入顺序,逐个截断;总量有字符预算)。"""
    lines = [f"{c.memory_id} | {c.kind} | {clamp_text(c.text, CANDIDATE_CHARS)}"
             + context_label(c.fact_context)
             for c in visible_candidates(candidates)]
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(SYSTEM_PROMPT),
            HumanMessage(USER_TEMPLATE.format(candidates="\n".join(lines) or "(无)",
                                              fact=clamp_text(fact, MAX_FACT_CHARS)))]


# ---- 决策 ----


def decide(fact: str, candidates: Sequence[Candidate], *, llm=None,
           retries: int | None = None, max_events: int | None = None) -> MaintenanceDecision:
    """问模型该如何维护;只对「输出不合协议」做有限重试(与提取层同一套口径)。

    - 结构错误(不是 JSON / 外层不符 / 事件项不合法)与授权错误(引用没展示过的 id /
      同一 id 多个事件 / 超限 / 混用 NONE)**都**走带纠正提示的有限重试 —— 这是模型可以
      自己改对的一类错误,重试一次往往就好了;
    - 重试用尽后:结构错误抛 MemoryExtractionError、授权错误抛 MemoryDecisionRejected
      (调用方据此判定「整条决策不生效」,绝不部分执行);
    - 业务异常(网络 / 超时 / 4xx)原样上抛,由任务重试与退避负责,不在付费调用上打转。
    """
    from app.memory.llm import get_memory_llm

    if llm is None:
        llm = get_memory_llm()
    if retries is None:
        retries = max(0, int(get_settings().memory_maintenance_retries))
    limit = int(get_settings().memory_max_text_chars)
    allowed = {c.memory_id for c in visible_candidates(candidates)}

    prompt = build_messages(fact, candidates)
    attempts = 0
    reason = ""
    while True:
        attempts += 1
        response = llm.invoke(prompt)
        try:
            decision = _parse(content_of(response), limit=limit)
            authorize(decision, allowed_ids=allowed, max_events=max_events)
        except (MemoryExtractionError, MemoryDecisionRejected) as exc:
            reason = str(exc)
            code = type(exc).__name__
            if attempts > retries:
                if isinstance(exc, MemoryDecisionRejected):
                    raise MemoryDecisionRejected(
                        f"维护决策不合协议(共调用 {attempts} 次):{reason}") from None
                raise MemoryExtractionError(
                    f"维护决策无法解析(共调用 {attempts} 次):{reason}") from None
            from langchain_core.messages import HumanMessage
            logger.warning("维护决策不合协议,第 %d 次重试:%s(%s)", attempts, reason, code)
            prompt = [*prompt, response, HumanMessage(_CORRECTIVE.format(reason=reason))]
            continue
        decision.attempts = attempts
        return decision


def dump(decision: MaintenanceDecision) -> str:
    """把决策序列化成可入库的 JSON(阶段结果复用用;正文本来就在库里,不进日志)。"""
    events = []
    for event in decision.events:
        payload = event.model_dump(mode="json", exclude={"event_time"})
        if getattr(event, "event_time", None) is not None:
            payload["event_time"] = event.event_time.strftime("%Y-%m-%dT%H:%M:%S")
        events.append(payload)
    return json.dumps({"events": events, "reason": decision.reason}, ensure_ascii=False)


def restore(text: str, *, allowed_ids: set[str],
            max_events: int | None = None) -> MaintenanceDecision:
    """把**存下来的**决策按同一套协议重新校验一遍,通过才允许执行。

    重试用它来避免重复付费(见 worker 的阶段复用),但复用不等于免检:id 授权、事件上限、
    秘密闸、结构校验一条都不跳过 —— 存下来的决策同样可能因为候选变化或协议升级而失效,
    那时抛异常,由调用方重新决策。
    """
    decision = _parse(text, limit=int(get_settings().memory_max_text_chars))
    authorize(decision, allowed_ids=allowed_ids, max_events=max_events)
    return decision


def authorize(decision: MaintenanceDecision, *, allowed_ids: set[str],
              max_events: int | None = None) -> MaintenanceDecision:
    """校验整条决策是否**整体可执行**;任何一处不合法就抛 MemoryDecisionRejected。

    为什么不是「丢掉那一条、其余照做」:落库时每个事件都改真实记忆,「一半生效」比「一次
    都不生效」危险得多 —— DELETE 旧说法 + ADD 新说法只做成前半截,用户就永久丢了内容;
    超限时先做掉前 5 个事件,同样是把一次越权输出当成了部分授权。所以这里的规则是:

    - 事件里的 id 必须在 allowed_ids 内(授权候选 ← **本次真正展示给模型的那批**);
    - 同一个 id 最多被一个事件动(删了又改 / 改两次一律算不合法);
    - 事件总数不超过 max_events(默认取配置):单条事实不该引发大面积改写;
    - 秘密形态的正文(ADD / UPDATE)、空正文一律不合法 —— 与提取层同一道闸;
    - NONE 与其它事件混在一起是自相矛盾(既说「不用动」又要改):不合法。

    合法但重复的 NONE 会被合并成一条(记进 dropped 说明),那是规范化,不是丢弃事件。
    """
    if max_events is None:
        max_events = max(1, int(get_settings().memory_maintenance_max_actions))
    kept: list = []
    notes: list[str] = []
    touched: set[str] = set()
    changes = 0
    none_seen = False
    for event in decision.events:
        if isinstance(event, NoneEvent):
            if changes:                      # 后面已经出现过要执行的改动:自相矛盾
                raise MemoryDecisionRejected(
                    "决策里同时出现了 NONE 与要执行的改动(自相矛盾),整条不采纳")
            if none_seen:                    # 重复的 NONE:规范化,不是丢事件
                notes.append("重复的 NONE 已合并为一条")
                continue
            none_seen = True
            kept.append(event)
            continue
        if none_seen:                        # 顺序反过来(NONE 在前)同样是自相矛盾
            raise MemoryDecisionRejected(
                "决策里同时出现了 NONE 与要执行的改动(自相矛盾),整条不采纳")
        if event.event in (EVENT_UPDATE, EVENT_DELETE):     # 要动已有的记忆:必须授权
            if event.id not in allowed_ids:
                raise MemoryDecisionRejected(
                    "事件引用了本次没有展示给模型的候选 id,整条决策不采纳")
            if event.id in touched:
                raise MemoryDecisionRejected(
                    "同一个 id 被多个事件引用(删了又改 / 改两次),整条决策不采纳")
        if event.event in (EVENT_ADD, EVENT_UPDATE):        # 要写正文:过秘密闸与空正文闸
            if looks_secret(event.text):
                raise MemoryDecisionRejected("事件正文疑似包含密码 / 密钥 / 令牌等秘密,整条决策不采纳")
            if not normalize_text(event.text):
                raise MemoryDecisionRejected("事件正文为空,整条决策不采纳")
        if changes >= max_events:
            raise MemoryDecisionRejected(
                f"事件数超过上限({max_events}),整条决策不采纳(不做部分执行)")
        if event.event in (EVENT_UPDATE, EVENT_DELETE):
            touched.add(event.id)                 # ADD 不带 id,不占「同一 id」额度
        changes += 1
        kept.append(event)
    decision.events = kept
    decision.dropped.extend(notes)
    return decision


# ---- 解析与逐项校验 ----


def _parse(content: str, *, limit: int) -> MaintenanceDecision:
    """解析 + 逐项校验。

    与提取层刻意不同:**任何一个事件不合法,整份输出就是不合法**(抛
    MemoryExtractionError → 走纠正重试)。提取层丢一条坏条目还剩别的可用事实,而这里
    丢一条坏事件就变成「决策只执行了一半」—— 那是会改坏用户记忆的。
    """
    raw = strip_fence(content)
    if not raw:
        raise MemoryExtractionError("模型返回了空内容")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MemoryExtractionError(f"不是合法 JSON({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise MemoryExtractionError("顶层不是 JSON 对象")
    try:
        envelope = MaintenanceEnvelope.model_validate(data)
    except ValidationError as exc:
        raise MemoryExtractionError(f"外层结构不符({brief(exc)})") from None

    events: list = []
    for entry in envelope.events:
        try:
            events.append(_EVENT_ADAPTER.validate_python(entry))
        except ValidationError as exc:
            raise MemoryExtractionError(f"事件结构不合法({brief(exc)})") from None
    clipped = []
    for event in events:
        if isinstance(event, (AddEvent, UpdateEvent)):
            # 入库口径的截断(校验的硬上限在 MAX_FACT_CHARS)
            event = event.model_copy(update={"text": clamp_text(event.text, limit)})
            if not normalize_text(event.text):
                raise MemoryExtractionError("事件正文为空") from None
        clipped.append(event)
    if not clipped:
        # {"events": []} 是**合法**的显式空决策(什么都不用做),不是格式错误;
        # 与 `{}`(没有 events 字段 → 外层结构错误)区分开。
        return MaintenanceDecision(events=[], dropped=[],
                                   reason=clamp_text(envelope.reason or "", REASON_CHARS)
                                   or "模型给出了空决策(没有需要执行的改动)")
    return MaintenanceDecision(events=clipped, dropped=[],
                               reason=clamp_text(envelope.reason or "", REASON_CHARS))
