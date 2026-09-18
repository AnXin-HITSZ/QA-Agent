"""LLM 工厂:从 .env 建 ChatOpenAI(OpenAI 兼容端点)。

上层(图节点 / 工具)只依赖 get_llm() 返回的 BaseChatModel,换厂商只改 .env。
ReAct 图依赖模型的 tool-calling 能力,因此必须配置真实 LLM_API_KEY。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import get_settings


@lru_cache
def get_llm() -> BaseChatModel:
    s = get_settings()

    if not s.llm_api_key:
        raise RuntimeError(
            "未配置 LLM_API_KEY:请在 backend/.env 设置后再启动"
            "(ReAct 图依赖模型的 tool-calling,需接入真实模型)。"
        )

    # 任意 OpenAI 兼容端点(OpenAI / DeepSeek / 千问 / 智谱 / Kimi)。
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=s.llm_temperature,
        timeout=s.llm_request_timeout,
    )
