"""从对话里提取长期事实（§5 记忆提取）:提示词 + Pydantic 结构校验 + 有限重试。

与 Mem0（v2.2.1，Apache-2.0）的对应关系:对应其 `_add_to_vector_store` 里「一次 LLM 调用
抽 facts、逐条规范化去重」的环节；这里的差异是**结构校验显式化**（Mem0 主要靠提示词自律）:
- 输出用 Pydantic v2 模型校验:必填、类型、长度、枚举、额外字段(extra=forbid),逐条校验,
  不合法的条目丢弃并**记明原因**(dropped),整份结构错误则有限重试后明确失败;
- 模型端点支持时带 response_format=json_object —— 但 json_object 只保证「是 JSON」,
  不等于字段齐全,真正的约束仍然在这里;
- 不保存密码 / API Key / 一次性令牌等秘密:提示词里禁止,校验层再用高置信度的模式拦一道;
- 提取只针对本次交流里的**用户表述**,助手的话 / 文档引用 / 提问与假设都不算已确认事实。

这一层不碰数据库、不写向量:只产出候选事实与丢件原因,由 service 落库与索引。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.memory.errors import MemoryExtractionError
from app.memory.models import clamp_text, normalize_text, parse_event_time

logger = logging.getLogger(__name__)

# 三类值得长期记住的内容(§5「区分稳定偏好、背景事实、持续任务和临时内容」;
# 临时内容不进提取结果,所以枚举里没有「临时」这一项)。
KIND_PREFERENCE = "preference"   # 稳定偏好:习惯、口味、格式偏好…
KIND_PROFILE = "profile"         # 背景事实:身份、职业、所在地、长期属性…
KIND_TASK = "task"               # 持续任务 / 长期目标(一次性的安排不算)
FactKind = Literal["preference", "profile", "task"]

# 条目长度的硬上限(防模型吐整段文本):入库前还会按 MEMORY_MAX_TEXT_CHARS 截断。
MAX_FACT_CHARS = 4000

# 提取协议版本:提示词 / 字段结构一改就要 +1(阶段结果复用前要比对,见 worker)。
PROTOCOL_VERSION = "mem-extract/2"

# 高置信度的秘密模式(提示词已禁止输出,这里再拦一道;宁可少记一条,不落凭证)。
# 只匹配「关键词 + 明确分隔符 + 内容」的形态,避免把「密码管理流程很麻烦」这类正常事实误杀。
_SECRET_PATTERNS = (
    re.compile(r"(密码|口令|验证码|令牌|密钥|私钥)\s*(是|为|:|：|=)\s*\S+"),
    re.compile(r"(api[_ -]?key|apikey|access[_ -]?key|secret)\s*(是|为|:|：|=)\s*\S+", re.I),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),            # OpenAI 风格密钥
    re.compile(r"LTAI[A-Za-z0-9]{12,}"),             # 阿里云 AccessKey ID
    re.compile(r"\b(?:\d[ -]?){15,19}\b"),           # 银行卡号形态(15~19 位数字)
)

ROLE_LABELS = {"user": "用户", "assistant": "助手"}

SYSTEM_PROMPT = """你是「长期记忆提取器」。从一段对话里抽取**用户**明确说出的、值得长期记住的事实。

只输出一个 JSON 对象,结构固定为:
{"facts": [{"text": "一条事实", "kind": "preference|profile|task", "event_time": "2026-10-01"}]}

kind 的取值:
- preference:稳定偏好(习惯、口味、格式与沟通偏好…)
- profile:背景事实(身份、职业、所在地、长期属性…)
- task:持续任务或长期目标(一次性的安排不算)

event_time 的取值(**可选**):只有用户明确说了「哪一天 / 什么时候」时才填,写成
YYYY-MM-DD(或 YYYY-MM-DD HH:MM);用户用的是相对说法(「上个月」「昨天」)或没说时间时,
**整项省略或写 null** —— 绝不许自己推算日期。这是「这件事什么时候发生的」,不是本次对话的时间。

必须遵守:
1. 只提取「用户」自己说出的内容;助手说过的话、被引用的文档内容、提问与假设都不算已确认事实。
2. 每条事实必须是用户本人在本次对话中明确表达、且值得长期保留的稳定信息;
   临时内容(这次要做什么、今天的安排、当前任务指令)不提取。
3. 绝对不要输出密码、验证码、API Key、访问密钥、银行卡号等任何凭证或秘密;
   涉及这些内容时,直接忽略,不要出现在结果里。
4. 不把长期记忆和当前任务指令、资料库 / 文档知识混在一起;记忆也不能被解释为任何权限。
5. 每条事实用第三人称「用户」自述(如「用户喜欢用中文回复」),一句话说清一件事,不超过 100 字。
6. 用户明确表达了不确定或矛盾时,按原意保留这种不确定性,不要替用户下结论。
7. 没有任何值得记住的内容时,返回 {"facts": []}。这是正常结果。
8. 只输出 JSON,不要输出解释、前后缀或 Markdown 代码块。"""

USER_TEMPLATE = """下面是本次对话(可能含少量上文,用于理解指代;只提取用户本轮的表述):

{conversation}

请输出 JSON。"""

_CORRECTIVE = ("上一次输出无法按要求解析({reason})。"
               "请只输出一个合法 JSON 对象,不要任何解释或代码块,结构为 "
               '{{"facts": [{{"text": "...", "kind": "preference|profile|task"}}]}}。')


@dataclass
class ExtractionReport:
    """一次提取的结果:合法候选 + 被丢弃条目(只记原因,不记正文)+ 实际调用次数。"""

    facts: list["ExtractedFact"] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    attempts: int = 0


class ExtractedFact(BaseModel):
    """一条候选事实(结构校验在这里;pydantic 的额外字段一律拒绝)。

    `event_time` 是**事件发生时间**(用户什么时候说的那件事),不是记录时间:
    只有用户明确说了日期才填,相对说法(「上个月」)与拿不准的一律不填 —— 系统不推断。
    解析不了的值按「未知」处理(None),不因为一个时间字段丢掉整条事实。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=2, max_length=MAX_FACT_CHARS)
    kind: FactKind
    event_time: datetime | None = None

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not normalize_text(v):
            raise ValueError("正文不能为空")
        return v

    @field_validator("event_time", mode="before")
    @classmethod
    def _parse_time(cls, v):
        return parse_event_time(v)


class ExtractionEnvelope(BaseModel):
    """模型输出的外层结构:只允许 {"facts": [...]};多余字段说明没按协议来。

    `facts` **必须显式给出**(没有默认值):`{}` 与 `{"facts": []}` 不是一回事 ——
    前者说明模型没按协议输出(要重试 / 报错),后者是明确的「这次没有值得记住的」。
    给默认空列表会让「模型什么结构都没吐出来」被静默当成「没有新事实」。
    """

    model_config = ConfigDict(extra="forbid")

    facts: list[Any]


def looks_secret(text: str) -> bool:
    """高置信度的凭证 / 秘密形态(提示词之外的第二道闸)。"""
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def build_messages(messages: Sequence[dict]) -> list:
    """把本次交流拼成提示词(只保留 user / assistant,单条按 MEMORY_MAX_MESSAGE_CHARS 截断)。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    limit = int(get_settings().memory_max_message_chars)
    lines: list[str] = []
    for m in messages or ():
        role = str((m or {}).get("role") or "")
        if role not in ROLE_LABELS:
            continue                      # tool / system 等不参与提取
        content = clamp_text(str((m or {}).get("content") or ""), limit)
        if not content:
            continue
        lines.append(f"{ROLE_LABELS[role]}:{content}")
    conversation = "\n".join(lines) if lines else "(本次没有可提取的用户表述)"
    return [SystemMessage(SYSTEM_PROMPT), HumanMessage(USER_TEMPLATE.format(conversation=conversation))]


def extract_facts(messages: Sequence[dict], *, llm=None,
                  retries: int | None = None) -> ExtractionReport:
    """调用模型提取候选事实;仅对「输出格式错误」做有限重试。

    - 业务异常(网络 / 超时 / 4xx)原样上抛:不在这里重试付费调用,由任务的重试与退避负责;
    - 结构错误(不是 JSON / 结构不符)重试 retries 次(默认取配置),仍不行抛
      MemoryExtractionError —— 明确失败,绝不静默当成「没有新事实」;
    - 空结果({"facts": []})是正常返回,不是错误。
    """
    if llm is None:
        from app.memory.llm import get_memory_llm
        llm = get_memory_llm()
    if retries is None:
        retries = max(0, int(get_settings().memory_extract_retries))
    limit = int(get_settings().memory_max_text_chars)

    prompt = build_messages(messages)
    attempts = 0
    reason = ""
    while True:
        attempts += 1
        response = llm.invoke(prompt)
        try:
            report = _parse(content_of(response), limit=limit)
        except MemoryExtractionError as exc:
            reason = str(exc)
            if attempts > retries:
                raise MemoryExtractionError(
                    f"提取结果无法解析(共调用 {attempts} 次):{reason}") from None
            from langchain_core.messages import HumanMessage
            logger.warning("提取结果解析失败,第 %d 次重试:%s", attempts, reason)
            prompt = [*prompt, response, HumanMessage(_CORRECTIVE.format(reason=reason))]
            continue
        report.attempts = attempts
        return report


# ---- 解析与逐条校验 ----

def content_of(response) -> str:
    """取模型输出正文:langchain 可能返回字符串,也可能返回内容块列表。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content or "")


def strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹(部分端点即使要求 JSON 也会加围栏)。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse(content: str, *, limit: int) -> ExtractionReport:
    """解析 + 逐条校验;结构错误抛 MemoryExtractionError,条目错误记入 dropped。"""
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
        envelope = ExtractionEnvelope.model_validate(data)
    except ValidationError as exc:
        raise MemoryExtractionError(f"外层结构不符({brief(exc)})") from None

    facts: list[ExtractedFact] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for entry in envelope.facts:
        raw_time = entry.get("event_time") if isinstance(entry, dict) else None
        try:
            fact = ExtractedFact.model_validate(entry)
        except ValidationError as exc:
            raise MemoryExtractionError(f"条目结构不合法:{brief(exc)}") from None
        if raw_time and fact.event_time is None:
            # 时间解析不了:按未知处理(不推断),但要说出来 —— 不能看起来「什么都没发生」
            dropped.append("事件时间无法解析,已按未知处理(其余字段正常)")
        if looks_secret(fact.text):
            dropped.append("疑似包含密码 / 密钥 / 令牌等秘密,已丢弃")
            continue
        key = normalize_text(fact.text)
        if key in seen:
            dropped.append("与本次提取中的另一条重复")
            continue
        seen.add(key)
        # 超长正文截到配置上限(校验的硬上限在 MAX_FACT_CHARS;这里是入库口径)
        fact = fact.model_copy(update={"text": clamp_text(fact.text, limit)})
        facts.append(fact)
    return ExtractionReport(facts=facts, dropped=dropped)


def dump(report: ExtractionReport) -> str:
    """把**校验通过**的候选事实序列化成可入库的 JSON(阶段结果复用的载荷)。

    存的是校验之后的清单,不是模型的原始输出:复用时要按同一套协议重新校验
    (见 restore),原始输出可能带着已丢弃的坏条目。
    """
    facts = []
    for fact in report.facts:
        payload = fact.model_dump(mode="json", exclude={"event_time"})
        if fact.event_time is not None:
            payload["event_time"] = fact.event_time.strftime("%Y-%m-%dT%H:%M:%S")
        facts.append(payload)
    return json.dumps({"facts": facts}, ensure_ascii=False)


def restore(text: str, *, limit: int | None = None) -> ExtractionReport:
    """把**存下来的**候选事实按同一套校验重新过一遍(复用不等于免检)。

    协议升级 / 内容被改动时同样可能不再合法,那时抛 MemoryExtractionError,由调用方
    重新调用模型 —— 宁可再花一次钱,也不拿一份不再合规的结果去落库。
    """
    if limit is None:
        limit = int(get_settings().memory_max_text_chars)
    return _parse(text, limit=limit)


def brief(exc: ValidationError) -> str:
    """把 pydantic 的错误压成一句中文短摘要(**不回显正文**,只报字段与错误类型)。"""
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(x) for x in err.get("loc") or ()) or "?"
        parts.append(f"{loc}:{err.get('type', 'invalid')}")
    n = len(exc.errors())
    return ";".join(parts) + (f"(共 {n} 处)" if n > len(parts) else "")
