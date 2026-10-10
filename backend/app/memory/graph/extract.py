"""图提取（mem-graph-extract/2）:把已经入库的事实整理成实体 / 关系 / 事件。

为什么是独立的第二个阶段、而不是塞进 mem-extract/5（方案 §提取与一致性）:
- 事实提取管「值不值得记」,图提取管「这些事实之间是什么结构」——两次调用各司其职,
  改图提示词不会动已经稳定的提取协议（不回归已有行为）;
- **一致性靠构造而不是靠自觉**:图提取的输入是已入库事实的编号清单,模型输出的每条
  关系 / 事件都必须引用事实编号;后端再逐项校验「名称 / 宾语 / 参与者 / 属性值都能在
  被引用事实的正文里找到」。这些检查约束内容出处，不证明模型的关系判断必然正确；
  引用与摘录仍需结合来源和效果评测检查，不能把名称共现当作关系成立;
- 事件描述不自造:模型只指认 description_fact（信息最完整、时间最新的一条事实）,
  正文由后端从那条事实原样复制;时间 / 状态也由后端从被引用事实复制（temporal 口径
  与事实提取完全一致,不在这里重新解释时间）。

本层与 extract.py 同一姿态:提示词 + Pydantic 严格校验 + 有限重试;不碰数据库、
不碰 Neo4j —— 产出经过校验的图元素清单与丢件原因，落库在 graph/runtime.py。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings
from app.memory.errors import MemoryExtractionError
from app.memory.extract import ROLE_LABELS
from app.memory.graph.models import name_key
from app.memory.models import clamp_text

logger = logging.getLogger(__name__)

# 图提取协议版本:提示词 / 字段结构一改就要 +1(阶段结果复用前要比对,见 worker)。
PROTOCOL_VERSION = "mem-graph-extract/2"

# 单条图元素的规模上限(防模型吐一整段;超限按「截断并记原因」处理,不静默)。
MAX_ALIASES_PER_ENTITY = 8
MAX_PARTICIPANTS_PER_EVENT = 12
MAX_ATTRIBUTES_PER_EVENT = 8
MAX_ENTITY_NAME_CHARS = 120
MAX_OBJECT_TEXT_CHARS = 300
MAX_RELATION_TYPE_CHARS = 64
MAX_ROLE_CHARS = 64
MAX_EVENT_KIND_CHARS = 24
MAX_ATTRIBUTE_KEY_CHARS = 40
MAX_ATTRIBUTE_VALUE_CHARS = 200
MAX_FACTS_PER_ITEM = 25

# 整份响应的数量上限(单轮对话远到不了;只是防失控,触顶会如实记入 dropped)。
MAX_ENTITIES = 60
MAX_RELATIONS = 120
MAX_EVENTS = 60

# 与 models.KNOWN_ENTITY_KINDS 必须一致(tests/test_memory_graph_extract.py 有看护)。
EntityKind = Literal["person", "place", "org", "project", "product", "animal", "event", "topic", "unknown"]

SYSTEM_PROMPT = """你是记忆图整理器。输入是已入库的事实（带编号）与出处对话节选。
把这些事实整理成实体、关系、事件，并注明每条图元素来自哪些事实编号。
铁律：
- 内容只能来自事实清单：实体名、别名、关系宾语、参与者、属性值都必须能在事实正文里找到；
  指代可以在本批事实内消解，但姓名必须出自事实。
- 每条关系与事件必须用 facts 引用至少一个事实编号；不得引用清单之外的编号。
- 实体：每条声明必须提供批内唯一 ref（例如 e1）；关系主语和实体宾语引用 ref，不能用名称代替身份。同一实体的不同说法写一条，其它说法放 aliases；同一个名字在事实里明显指不同对象时
  分开写，语义相近但不同的名字不要合并。kind 只能用 person/place/org/project/product/animal/event/topic/unknown。
- 关系：方向按事实原文的主语→宾语，不得反转；必须给出支持该关系的 quote 原文摘录。名称共现不代表关系成立，不确定时不输出关系。宾语是实体时写 object_entity（entities 中的 ref），是原文短语时写 object_text，二者只写其一。
- 事件：描述不要改写——description_fact 填引用事实里信息最完整、时间最新的一条的事实编号，
  系统会用那条事实正文作为事件描述；同一次事件的重复提及合并成一条，把全部编号写进 facts；
  不同日期、不同计划状态（打算去 / 去过了）的事实必须分开，不得合并。
- 参与者：写事实原文里的名称与角色（role 用 subject/participant/companion/organizer/recipient/giver/other 之类通用词）。
- attributes 只放事实正文里明确写出的键值（如金额、地点），没有就空对象；不要保存密码、密钥等秘密。
输出结构（占位符仅说明字段，不提供领域示例）：
{"entities":[{"ref":"e1","name":"<原文实体名称>","kind":"unknown","aliases":[],"facts":[0]}],
"relations":[{"subject":"e1","relation":"<原文支持的关系>","object_entity":"","object_text":"<原文宾语>","facts":[0],"quote":"<支持关系的原文摘录>"}],
"events":[{"kind":"unknown","description_fact":0,"facts":[0],"participants":[{"name":"<原文参与者>","entity_ref":"e1","role":"participant"}],"attributes":{}}]}
没有可整理的内容返回 {"entities":[],"relations":[],"events":[]}。只输出 JSON，不要 Markdown 或解释。"""

USER_TEMPLATE = """下面是本次对话（仅用于理解指代，不是内容来源）：

{conversation}

下面是**已经入库的事实**（唯一的内容来源；引用时只用编号）：

{facts}

请输出 JSON。"""

_CORRECTIVE = ("上一次输出无法按要求解析({reason})。"
               "请只输出一个合法 JSON 对象,不要任何解释或代码块,结构为 "
               '{{"entities": [{{"ref": "e1", "name": "...", "kind": "person", "aliases": [], "facts": [0]}}], '
               '"relations": [{{"subject": "...", "relation": "...", "object_entity": "", '
               '"object_text": "...", "facts": [0], "quote": "原文摘录"}}], '
               '"events": [{{"kind": "...", "description_fact": 0, "facts": [0], '
               '"participants": [], "attributes": {{}}}}]}}。')


@dataclass
class GraphReport:
    """一次图提取的结果:合法图元素 + 被丢弃条目(只记原因,不记正文)+ 实际调用次数。"""

    entities: list["GraphEntity"] = field(default_factory=list)
    relations: list["GraphRelation"] = field(default_factory=list)
    events: list["GraphEvent"] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    attempts: int = 0


class GraphEntity(BaseModel):
    """一个实体:规范名 + 类型 + 别名 + 来源事实编号（至少一条）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=MAX_ENTITY_NAME_CHARS)
    kind: EntityKind
    aliases: list[str] = Field(default_factory=list, max_length=MAX_ALIASES_PER_ENTITY)
    facts: list[int] = Field(min_length=1, max_length=MAX_FACTS_PER_ITEM)


class GraphRelation(BaseModel):
    """一条关系:主语（必须已声明实体）→ 类型 → 宾语（实体名或原文短语,二者其一）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=MAX_ENTITY_NAME_CHARS)
    relation: str = Field(min_length=1, max_length=MAX_RELATION_TYPE_CHARS)
    object_entity: str = Field(default="", max_length=MAX_ENTITY_NAME_CHARS)
    object_text: str = Field(default="", max_length=MAX_OBJECT_TEXT_CHARS)
    facts: list[int] = Field(min_length=1, max_length=MAX_FACTS_PER_ITEM)
    quote: str = Field(min_length=1, max_length=2000)


class GraphEventParticipant(BaseModel):
    """事件参与者:名称原文 + 角色（通用词,可扩展;不在这里绑定实体）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_ENTITY_NAME_CHARS)
    role: str = Field(default="participant", min_length=1, max_length=MAX_ROLE_CHARS)
    entity_ref: str = Field(default="", max_length=64)


class GraphEvent(BaseModel):
    """一个事件:类型 + 描述来源（description_fact）+ 全部提及（facts）+ 参与者 + 有依据属性。

    `description_fact` 必须 ∈ `facts`:描述从那条事实正文原样复制,模型不改写。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=MAX_EVENT_KIND_CHARS)
    description_fact: int = Field(ge=0)
    facts: list[int] = Field(min_length=1, max_length=MAX_FACTS_PER_ITEM)
    participants: list[GraphEventParticipant] = Field(
        default_factory=list, max_length=MAX_PARTICIPANTS_PER_EVENT)
    attributes: dict[str, str] = Field(default_factory=dict,
                                       max_length=MAX_ATTRIBUTES_PER_EVENT)


class GraphEnvelope(BaseModel):
    """模型输出的外层结构:三个键**必须显式给出**（理由同 extract.ExtractionEnvelope）。"""

    model_config = ConfigDict(extra="forbid")

    entities: list[Any]
    relations: list[Any]
    events: list[Any]


# ---- 提示词 ----

def _flatten(text: str) -> str:
    """比对用的归一:NFKC + casefold + 折叠空白（不改变语义,只抹平写法差异）。"""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(t.split())


def _grounded(surface: str, sources: Sequence[str]) -> bool:
    """surface 能否在被引用文本里找到(逐字,大小写 / 全半角不敏感)。"""
    key = _flatten(surface)
    return bool(key) and any(key in _flatten(s) for s in sources)


def _fact_line(index: int, fact) -> str:
    """一行事实:编号 + 结构标签 + 正文(引用时只用编号,标签帮模型挑时间与状态)。"""
    context = getattr(fact, "fact_context", None) or {}
    bits = [f"类型:{getattr(fact, 'kind', '')}"]
    state = getattr(fact, "state", "") or context.get("state", "")
    if state and state != "unknown":
        bits.append(f"状态:{state}")
    expression = getattr(fact, "time_expression", "") or (context.get("time") or {}).get("raw", "")
    if expression:
        bits.append(f"时间表述:{expression}")
    recorded = [str(s.get("recorded_at") or "") for s in (context.get("sources") or [])
                if s.get("recorded_at")]
    if recorded:
        bits.append(f"记录时间:{recorded[-1]}")
    return f"[事实{index}]({'；'.join(bits)}) {getattr(fact, 'text', '')}"


def build_messages(facts: Sequence, messages: Sequence[dict], note: str = "") -> list:
    """拼提示词:对话节选(理解指代用)+ 编号事实清单(唯一内容来源)。

    `note` 与 extract.build_messages 同一语义(评测适配说明):非空时作为第二条系统消息,
    默认空串 —— 正式路径行为与不带该参数完全一致。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    limit = int(get_settings().memory_max_message_chars)
    lines: list[str] = []
    for index, m in enumerate(messages or ()):
        role = str((m or {}).get("role") or "")
        if role not in ROLE_LABELS:
            continue
        content = clamp_text(str((m or {}).get("content") or ""), limit)
        if not content:
            continue
        mid = str(m.get("message_id") or f"m{index}")
        lines.append(f"[消息ID:{mid}]{ROLE_LABELS[role]}:{content}")
    conversation = "\n".join(lines) if lines else "(本次没有可参考的对话内容)"
    fact_lines = "\n".join(_fact_line(i, f) for i, f in enumerate(facts or ()))
    if not fact_lines:
        fact_lines = "(没有事实)"
    prompt = [SystemMessage(SYSTEM_PROMPT)]
    note = (note or "").strip()
    if note:
        prompt.append(SystemMessage(note))
    prompt.append(HumanMessage(USER_TEMPLATE.format(conversation=conversation, facts=fact_lines)))
    return prompt


def extract_graph(facts: Sequence, messages: Sequence[dict], *, llm=None,
                  retries: int | None = None, note: str = "") -> GraphReport:
    """调用模型整理图元素;仅对「输出格式错误」做有限重试。

    与 extract.extract_facts 同一套纪律:业务异常(网络 / 超时)原样上抛由任务重试与
    退避负责;结构错误重试 retries 次(默认取配置),仍不行抛 MemoryExtractionError;
    空结果({"entities": [], ...})是正常返回。事实为空时**不调用模型**,直接空报告。
    """
    if not list(facts or ()):
        return GraphReport()
    if llm is None:
        from app.memory.llm import get_memory_llm
        llm = get_memory_llm()
    if retries is None:
        retries = max(0, int(get_settings().memory_graph_extract_retries))

    prompt = build_messages(facts, messages, note=note)
    attempts = 0
    reason = ""
    while True:
        attempts += 1
        response = llm.invoke(prompt)
        try:
            report = _parse(content_of(response), facts=facts)
        except MemoryExtractionError as exc:
            reason = str(exc)
            if attempts > retries:
                raise MemoryExtractionError(
                    f"图提取结果无法解析(共调用 {attempts} 次):{reason}") from None
            from langchain_core.messages import HumanMessage
            logger.warning("图提取结果解析失败,第 %d 次重试:%s", attempts, reason)
            prompt = [*prompt, response, HumanMessage(_CORRECTIVE.format(reason=reason))]
            continue
        report.attempts = attempts
        return report


# ---- 解析与逐项校验 ----

def content_of(response) -> str:
    """取模型输出正文(langchain 可能返回字符串或内容块列表)。"""
    from app.memory.extract import content_of as _content_of
    return _content_of(response)


def strip_fence(text: str) -> str:
    from app.memory.extract import strip_fence as _strip_fence
    return _strip_fence(text)


def _check_indices(indices: Sequence[int], count: int, what: str) -> None:
    """事实编号必须在范围内 —— 越界说明模型没理解编号空间,按结构错误整份重试。"""
    if any(int(i) < 0 or int(i) >= count for i in indices):
        raise MemoryExtractionError(f"{what}引用了清单之外的事实编号")


def _parse(content: str, *, facts: Sequence) -> GraphReport:
    """解析 + 逐项校验;结构错误抛 MemoryExtractionError,内容对不上原文按丢弃处理。"""
    facts = list(facts or ())
    count = len(facts)
    texts = [getattr(f, "text", "") or "" for f in facts]
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
        envelope = GraphEnvelope.model_validate(data)
    except ValidationError as exc:
        from app.memory.extract import brief
        raise MemoryExtractionError(f"外层结构不符({brief(exc)})") from None

    report = GraphReport()

    # ---- 实体:名字必须出自事实(全批),批内同名合并(同一响应里的同一名字无法区分)----
    declared: dict[str, GraphEntity] = {}
    for entry in envelope.entities:
        try:
            entity = GraphEntity.model_validate(entry)
        except ValidationError as exc:
            from app.memory.extract import brief
            raise MemoryExtractionError(f"实体条目结构不合法:{brief(exc)}") from None
        _check_indices(entity.facts, count, "实体")
        if not _grounded(entity.name, texts):
            report.dropped.append("实体名在事实正文里找不到,已丢弃(不得引入清单外内容)")
            continue
        key = entity.ref or name_key(entity.name)
        entity = entity.model_copy(update={"ref": key})
        existing = declared.get(key)
        if existing is not None:
            # 只合并相同响应 ID；同名不同 ref 必须保留为独立实体。
            if name_key(existing.name) != name_key(entity.name) or existing.kind != entity.kind:
                raise MemoryExtractionError("同一实体 ref 对应不同名称或类型")
            merged_facts = sorted(set(existing.facts) | set(entity.facts))
            merged_aliases = list(existing.aliases)
            for alias in entity.aliases:
                if alias not in merged_aliases:
                    merged_aliases.append(alias)
            declared[key] = existing.model_copy(
                update={"facts": merged_facts,
                        "aliases": merged_aliases[:MAX_ALIASES_PER_ENTITY]})
            continue
        declared[key] = entity
    if len(declared) > MAX_ENTITIES:
        for key in list(declared)[MAX_ENTITIES:]:      # dict 保序:丢的是后面的
            del declared[key]
        report.dropped.append(f"实体数超过上限 {MAX_ENTITIES},超出部分已丢弃")

    # 别名:在被引用事实里能找到才保留;与任一实体规范名相同视为冗余写法(跳过);
    # 同一别名挂在两个实体上:第一条保留,后面的丢弃(绝不因为一个别名就把两个实体并起来)。
    alias_owner: dict[str, str] = {}
    entities: list[GraphEntity] = []
    for key, entity in declared.items():
        aliases: list[str] = []
        seen: set[str] = set()
        for alias in entity.aliases:
            alias = alias.strip()
            if not alias:
                continue
            akey = name_key(alias)
            if akey == name_key(entity.name) or akey in seen:
                continue                                  # 与规范名相同 / 批内重复:冗余写法
            if any(akey == name_key(e.name) for k, e in declared.items() if k != key):
                report.dropped.append(f"别名与另一实体的规范名相同,已丢弃")
                continue
            if akey in alias_owner:
                report.dropped.append(f"别名已挂在实体「{alias_owner[akey]}」上,本处已丢弃")
                continue
            if not _grounded(alias, [texts[i] for i in entity.facts]):
                report.dropped.append(f"实体「{entity.name}」的别名在事实正文里找不到,已丢弃")
                continue
            seen.add(akey)
            alias_owner[akey] = entity.name
            aliases.append(alias)
        entities.append(entity.model_copy(update={"aliases": aliases}))

    # ---- 关系:主语 / 宾语实体必须是已声明实体;宾语二选一;object_text 必须出自被引用事实 ----
    relation_keys: set[tuple] = set()
    relations: list[GraphRelation] = []
    for entry in envelope.relations:
        try:
            relation = GraphRelation.model_validate(entry)
        except ValidationError as exc:
            from app.memory.extract import brief
            raise MemoryExtractionError(f"关系条目结构不合法:{brief(exc)}") from None
        _check_indices(relation.facts, count, "关系")
        subject_key = relation.subject if relation.subject in declared else name_key(relation.subject)
        object_ref = relation.object_entity if relation.object_entity in declared else name_key(relation.object_entity)
        if subject_key not in declared:
            report.dropped.append("关系主语不是已声明的实体,已丢弃")
            continue
        has_entity = bool(relation.object_entity)
        has_text = bool(relation.object_text)
        if has_entity and has_text:
            report.dropped.append("关系宾语同时给了实体与短语(二选一),已丢弃")
            continue
        if not has_entity and not has_text:
            report.dropped.append("关系缺少宾语,已丢弃")
            continue
        if has_entity and object_ref not in declared:
            report.dropped.append("关系宾语不是已声明的实体,已丢弃")
            continue
        sources = [texts[i] for i in relation.facts]
        if not _grounded(declared[subject_key].name, sources):
            report.dropped.append("关系主语不在引用事实中，保留未决而不绑定")
            continue
        if has_entity and not _grounded(declared[object_ref].name, sources):
            report.dropped.append("关系宾语实体不在引用事实中，保留未决而不绑定")
            continue
        if relation.quote and not _grounded(relation.quote, sources):
            raise MemoryExtractionError("关系证据摘录与引用事实不匹配")
        relation = relation.model_copy(update={"subject": subject_key,
                    "object_entity": object_ref if has_entity else ""})
        if has_text and not _grounded(relation.object_text, sources):
            report.dropped.append("关系宾语短语在被引用事实里找不到,已丢弃")
            continue
        object_key = (name_key(relation.object_entity) if has_entity
                      else name_key(relation.object_text))
        dedupe = (name_key(relation.subject), name_key(relation.relation), object_key)
        if dedupe in relation_keys:
            for i, old in enumerate(relations):
                if (name_key(old.subject), name_key(old.relation),
                    name_key(old.object_entity) if old.object_entity else name_key(old.object_text)) == dedupe:
                    relations[i] = old.model_copy(update={"facts": sorted(set(old.facts + relation.facts))})
                    break
            report.dropped.append("同一条关系重复，已合并来源")
            continue
        relation_keys.add(dedupe)
        relations.append(relation)
    if len(relations) > MAX_RELATIONS:
        relations = relations[:MAX_RELATIONS]
        report.dropped.append(f"关系数超过上限 {MAX_RELATIONS},超出部分已丢弃")

    # ---- 事件:description_fact ∈ facts;参与者 / 属性值必须出自被引用事实 ----
    events: list[GraphEvent] = []
    seen_events: dict[tuple, int] = {}
    for entry in envelope.events:
        try:
            event = GraphEvent.model_validate(entry)
        except ValidationError as exc:
            from app.memory.extract import brief
            raise MemoryExtractionError(f"事件条目结构不合法:{brief(exc)}") from None
        _check_indices([event.description_fact, *event.facts], count, "事件")
        if event.description_fact not in event.facts:
            report.dropped.append("事件的描述来源不在它引用的编号里,已丢弃")
            continue
        sources = [texts[i] for i in event.facts]
        participants = []
        for person in event.participants:
            if not _grounded(person.name, sources):
                report.dropped.append(f"事件参与者「{person.name}」在被引用事实里找不到,已丢弃")
                continue
            if person.entity_ref:
                entity = declared.get(person.entity_ref)
                if not entity or name_key(person.name) not in {name_key(entity.name), *map(name_key, entity.aliases)}:
                    raise MemoryExtractionError("事件参与者名称与实体引用不一致")
            else:
                possible = [e.ref for e in entities if name_key(person.name) == name_key(e.name)]
                if len(possible) == 1:
                    person = person.model_copy(update={"entity_ref": possible[0]})
            participants.append(person)
        attributes: dict[str, str] = {}
        for name, value in event.attributes.items():
            name = (name or "").strip()
            value = (value or "").strip()
            if not name or not value or len(name) > MAX_ATTRIBUTE_KEY_CHARS:
                report.dropped.append("事件属性为空或名过长,已丢弃")
                continue
            if not _grounded(value, sources):
                report.dropped.append(f"事件属性「{name}」的值在被引用事实里找不到,已丢弃")
                continue
            attributes[name] = clamp_text(value, MAX_ATTRIBUTE_VALUE_CHARS)
        event = event.model_copy(update={"participants": participants, "attributes": attributes})
        dedupe = (name_key(event.kind), event.description_fact)
        if dedupe in seen_events:
            # 同一响应里两条事件指向同一条描述来源:合并(参与者与来源取并集)
            pos = seen_events[dedupe]
            kept = events[pos]
            merged_facts = sorted(set(kept.facts) | set(event.facts))
            names = {name_key(p.name) for p in kept.participants}
            merged_people = list(kept.participants) + [p for p in event.participants
                                                       if name_key(p.name) not in names]
            events[pos] = kept.model_copy(
                update={"facts": merged_facts,
                        "participants": merged_people[:MAX_PARTICIPANTS_PER_EVENT]})
            continue
        seen_events[dedupe] = len(events)
        events.append(event)
    if len(events) > MAX_EVENTS:
        events = events[:MAX_EVENTS]
        report.dropped.append(f"事件数超过上限 {MAX_EVENTS},超出部分已丢弃")

    report.entities = entities
    report.relations = relations
    report.events = events
    return report


def dump(report: GraphReport) -> str:
    """把**校验通过**的图元素序列化成可复用的 JSON(阶段结果复用的载荷)。

    存的是校验之后的清单,不是模型的原始输出;复用时要按同一套协议重新校验(见 restore)。
    """
    def item(model: BaseModel) -> dict:
        return model.model_dump(mode="json")

    return json.dumps({"entities": [item(e) for e in report.entities],
                       "relations": [item(r) for r in report.relations],
                       "events": [item(e) for e in report.events]}, ensure_ascii=False)


def restore(text: str, *, facts: Sequence) -> GraphReport:
    """把**存下来的**图元素按同一套校验重新过一遍(复用不等于免检)。

    校验需要事实原文(内容出处的逐字比对),调用方传入当前输入的事实清单;对不上就抛
    MemoryExtractionError,由调用方重新调用模型 —— 宁可再花一次钱,也不落一份不再合规的结果。
    """
    return _parse(text, facts=facts)
