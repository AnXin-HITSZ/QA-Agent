"""长期记忆的聊天模型:复用现有 LLM_* 配置,但要 JSON 输出、要计量、要低温度。

与 app/llm.py 的关系:
- 同一个端点 / Key / 模型(不新增配置项),但**不共用实例** —— 聊天主链路不计量
  (.env 的计量节写明「不含聊天 LLM」),记忆的提取与维护决策要进调用日志(§13),
  所以这里单独建一个带 MeteredChat 包装的客户端;
- 温度固定 0.0:提取与决策是结构化任务,结果要可复现(评测阶段还要固定生成参数);
- 模型端点支持时带 response_format=json_object(§5「模型端点支持时结合 JSON Schema」;
  端点不支持时把 MEMORY_LLM_JSON_MODE 置 false)—— 但这**不等于**完整结构约束,
  真正的字段 / 类型 / 长度 / 枚举校验在 extract.py 的 Pydantic 模型里。

未配置 LLM_API_KEY 时直接抛错(与 app/llm.py 同一条原则:不放假兜底)。
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.metering.metered_llm import MeteredChat


@lru_cache
def get_memory_llm() -> MeteredChat:
    s = get_settings()

    if not s.llm_api_key:
        raise RuntimeError(
            "未配置 LLM_API_KEY:长期记忆的提取与维护决策需要一个可用的聊天模型"
            "(复用 backend/.env 里聊天那套 LLM_* 配置),配置后重启即可。"
        )

    # 任意 OpenAI 兼容端点(与 app/llm.py 相同)。
    from langchain_openai import ChatOpenAI

    # 计量:用量探针(响应钩子)+ 包装层。惰性 import,未用到记忆时不加载。
    from app.metering.probe import UsageProbe, probe_client, provider_of

    probe = UsageProbe()          # 与下面的 httpx 客户端同生命周期(闭包持有,不会被回收)

    model_kwargs = {}
    if s.memory_llm_json_mode:
        # 要求供应商返回 JSON 对象;结构约束仍由 Pydantic 校验兜底(见模块 docstring)。
        model_kwargs["response_format"] = {"type": "json_object"}

    inner = ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=0.0,
        timeout=s.llm_request_timeout,
        model_kwargs=model_kwargs,
        # 自带 httpx 客户端:响应钩子观测真实请求次数(与供应商报的 usage 兜底)。
        http_client=probe_client(probe),
    )

    # 计量包装:每次 invoke 记一条调用事件;关闭计量时逐字透传(见 metering/metered_llm)。
    return MeteredChat(inner, probe, provider=provider_of(s.llm_base_url),
                       target=s.llm_model, endpoint=s.llm_base_url)
