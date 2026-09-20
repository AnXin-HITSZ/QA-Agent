"""对话入口:非流式 /chat、SSE 流式 /chat/stream,以及 SOP 热重载。"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, Request
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.graph import get_graph
from app.graph.trace import used_sop_id, used_sources
from app.rag import oss
from app.schemas.chat import ChatRequest, ChatResponse, SourceCitation
from app.skills import loader

router = APIRouter(prefix=get_settings().api_prefix, tags=["chat"])


def _graph(request: Request):
    """优先用 lifespan 注入的带记忆图(app.state.graph);未注入(如离线测试)则回退到无 checkpointer 的 get_graph()。"""
    g = getattr(request.app.state, "graph", None)
    return g if g is not None else get_graph()


def _sign_sources(hits: list[dict]) -> list[SourceCitation]:
    """把检索命中转成带短时效签名 URL 的引用来源。

    sign_url 是本地 HMAC 计算(不发网络请求),在 async 处理器里直接调无害;OSS 未配置 /
    签名异常不致命 —— 仍返回来源元信息,只是 url 留空(前端据此不给可点链接)。
    """
    out: list[SourceCitation] = []
    for h in hits:
        key = h.get("oss_key")
        if not key:
            continue
        try:
            url = oss.knowledge_store().sign_url(key)
        except Exception:
            url = None
        out.append(
            SourceCitation(
                oss_key=key,
                source=h.get("source"),
                category=h.get("category"),
                score=h.get("score"),
                url=url,
            )
        )
    return out


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """一次性返回完整回复;从 ReAct 轨迹回填本次引用的 SOP。thread_id 续接跨轮记忆。"""
    thread_id = req.thread_id or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    result = await _graph(request).ainvoke({"messages": [HumanMessage(content=req.message)]}, config=config)
    messages = result["messages"]
    ai = messages[-1]
    content = ai.content if isinstance(ai.content, str) else str(ai.content)
    return ChatResponse(
        skill=used_sop_id(messages),
        content=content,
        thread_id=thread_id,
        sources=_sign_sources(used_sources(messages)),
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
    """SSE 流式:先发 meta(带 thread_id),随后 token/tool_call/tool_result 均带 step(agent 轮次号)+ done(附 skill)。

    step 从 0 起,每当 tools 节点跑完 +1 —— 下一轮 agent 的输出属于新的 step。前端据此
    把扁平事件流切成时间线:某 step 跟了 tool_call 即为思考段,一直没有 tool_call 的最后
    一段即为最终答案。后端不再判定「思考 vs 答案」,分类交给前端(见 useChat.ts)。
    """
    thread_id = req.thread_id or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    inputs = {"messages": [HumanMessage(content=req.message)]}
    graph = _graph(request)

    async def event_gen():
        # 先把本通对话的 thread_id 交给前端存下,后续提问回传即可续接记忆。
        yield {"event": "meta", "data": json.dumps({"thread_id": thread_id}, ensure_ascii=False)}
        seen: list = []  # 累积 agent + tools 产出的消息,done 时回读本次引用的 SOP 与知识库来源
        step = 0  # agent 轮次号:token / tool_call / 该轮 tool_result 共享它
        async for mode, data in graph.astream(inputs, stream_mode=["updates", "messages"], config=config):
            if mode == "messages":
                chunk, meta = data
                # 只转发 agent 节点的文本:tools 节点的 ToolMessage(SOP 正文、检索结果)也走
                # messages 流,必须挡掉,否则工具原料会灌进答案里。
                if (meta or dict()).get("langgraph_node") != "agent":
                    continue
                text = getattr(chunk, "content", "") or ""
                # 带 tool_call_chunks 的增量是在拼工具调用,不是答案文本。
                if text and not getattr(chunk, "tool_call_chunks", None):
                    yield {"event": "token", "data": json.dumps({"content": text, "step": step}, ensure_ascii=False)}
                continue
            # mode == "updates":{节点名: 该节点返回的状态增量}
            for node, update in (data or dict()).items():
                messages = (update or dict()).get("messages") or []
                if node == "agent":
                    seen.extend(messages)
                    for msg in messages:
                        for call in getattr(msg, "tool_calls", None) or []:
                            yield {"event": "tool_call", "data": json.dumps({"name": call.get("name"), "args": call.get("args") or dict(), "step": step}, ensure_ascii=False)}
                elif node == "tools":
                    seen.extend(messages)  # ToolMessage 带 artifact → done 时供 used_sources 读知识库来源
                    for msg in messages:
                        yield {"event": "tool_result", "data": json.dumps({"name": getattr(msg, "name", None), "tool_call_id": getattr(msg, "tool_call_id", None), "step": step}, ensure_ascii=False)}
                    # 工具跑完 → 下一轮 agent 属于新的 step。
                    step += 1
        sources = [s.model_dump() for s in _sign_sources(used_sources(seen))]
        yield {"event": "done", "data": json.dumps({"skill": used_sop_id(seen), "sources": sources}, ensure_ascii=False)}

    return EventSourceResponse(event_gen())


@router.post("/admin/sops/reload")
async def reload_sops() -> dict:
    """热重载 sops/ 目录,返回当前 Skill 数量。"""
    return {"reloaded": loader.reload()}
