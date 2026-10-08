"""对话入口:非流式 /chat、SSE 流式 /chat/stream,以及 SOP 热重载。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from app.auth.deps import auth_ready, require_admin, require_user
from app.auth.service import Principal
from app import conversations
from app.config import get_settings
from app.conversations import inflight
from app.conversations.store import MAX_TITLE_LENGTH
from app.graph import get_graph
from app.graph.trace import used_sop_id, used_sources, used_images
from app.memory import db as memory_db
from app.rag import oss
from app.schemas.chat import ChatRequest, ChatResponse, SourceCitation
from app.skills import loader
from app.todos import format_for_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix, tags=["chat"])


def _graph(request: Request):
    """优先用 lifespan 注入的带记忆图(app.state.graph);未注入(如离线测试)则回退到无 checkpointer 的 get_graph()。"""
    g = getattr(request.app.state, "graph", None)
    return g if g is not None else get_graph()


async def _todos_prompt(request: Request) -> str:
    """读未完成待办、渲染成注入 system prompt 的一段;待办是顺带增强,任何异常都不能拖垮聊天。"""
    store = getattr(request.app.state, "todos", None)
    if store is None:
        return ""
    try:
        return format_for_prompt(await store.list_open())
    except Exception:  # noqa: BLE001 —— 读待办失败就当没有,照常回答
        return ""


async def _memory_prompt(principal: Principal, question: str) -> str:
    """回答前召回该用户自己的长期记忆(§9),渲染成注入 system prompt 的参考块。

    - 只读、只在本次生成用:经 config 传给 agent 节点,不写进 state、不入 checkpoint ——
      否则检索结果会在 Redis 里一轮轮累积,旧记忆越滚越长;
    - 记忆是**软依赖**:关闭 / 未配库 / 向量或库故障、乃至任何意外异常,一律返回空串,
      聊天照常(故障细节在 memory.recall_for_answer 里记日志,不打扰用户);
    - 检索里的 Embedding / Qdrant / 重排序都是同步阻塞调用,必须丢线程池,不占事件循环。
    """
    try:
        from app.memory import service as memory_service

        recall = await memory_db.run(memory_service.recall_for_answer,
                              user_id=principal.user_id, query=question)
    except Exception as exc:  # noqa: BLE001 —— 记忆挂了不能拖垮聊天
        logger.warning("长期记忆召回失败(%s),本轮按无记忆回答", type(exc).__name__)
        return ""
    return recall.text


async def _remember_exchange(principal: Principal, thread_id: str, question: str,
                             answer: str, *, source: str) -> None:
    """回答**完整**结束后登记一次提取任务(§9):只登记,提取由后台 worker 跑。

    调用点就是「成功标准」:非流式在 ainvoke 正常返回之后、流式在整个事件流跑完之后
    (客户端中途断开 / 生成抛错时根本走不到这里)—— 残缺回答不会被当成一次完整交流。

    登记失败只记日志:答案已经交给用户了,这里再抛只会让一次成功的回答变成 500;
    代价是这一轮没记下来,用户可在「我的记忆」里手动补一条。
    """
    if not (answer or "").strip():
        return                                   # 空回答没有可提取的内容
    try:
        from app.memory import service as memory_service

        await memory_db.run(memory_service.enqueue_extraction, user_id=principal.user_id,
                     messages=[{"role": "user", "content": question},
                               {"role": "assistant", "content": answer}],
                     thread_id=thread_id, source=source)
    except Exception as exc:  # noqa: BLE001 —— 登记失败不影响这轮回答
        logger.warning("长期记忆登记失败(%s),本轮未登记提取任务", type(exc).__name__)


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


async def _resolve_thread(req: ChatRequest, principal: Principal) -> str:
    """定下这轮用哪条线程:**新会话由后端建**,带 thread_id 的必须确属本人。

    - 不带 thread_id:新建一条会话目录行(MySQL)再拼出线程 id —— 客户端指定的 id 一律不认,
      否则等于允许「往别人的线程里写」;
    - 带 thread_id:回 MySQL 核对归属与状态。不存在 / 不是你的 / 正在删,对外都是 404,
      而且**绝不自动建一条新会话**(静默换线程会让用户以为记忆还在)。
    """
    if not req.thread_id:
        conv = await conversations.create_for_user(user_id=principal.user_id,
                                                  title=req.message.strip()[:MAX_TITLE_LENGTH])
        return conv.thread_id
    owned = await conversations.owned_by_thread(thread_id=req.thread_id,
                                                user_id=principal.user_id)
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "conversation_not_found", "message": "会话不存在"},
        )
    return owned.thread_id


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_user)])
async def chat(req: ChatRequest, request: Request,
               principal: Principal = Depends(require_user)) -> ChatResponse:
    """一次性返回完整回复;从 ReAct 轨迹回填本次引用的 SOP。thread_id 续接跨轮记忆。"""
    thread_id = await _resolve_thread(req, principal)
    config = {"configurable": {"thread_id": thread_id,
                               "todos_prompt": await _todos_prompt(request),
                               "memory_prompt": await _memory_prompt(principal, req.message)}}
    # 登记在途生成:用户这时点「删除会话」,删除流程会先等这一次跑完再抹 Redis。
    async with inflight.track(thread_id):
        result = await _graph(request).ainvoke({"messages": [HumanMessage(content=req.message)]},
                                               config=config)
    messages = result["messages"]
    ai = messages[-1]
    content = ai.content if isinstance(ai.content, str) else str(ai.content)
    # 完整回答已经拿到:登记提取任务(失败不影响这轮回答,见 _remember_exchange)。
    await _remember_exchange(principal, thread_id, req.message, content, source="chat")
    return ChatResponse(
        skill=used_sop_id(messages),
        content=content,
        thread_id=thread_id,
        sources=_sign_sources(used_sources(messages)),
        images=used_images(messages),
    )


async def _guarded(agen: AsyncIterator[dict], thread_id: str) -> AsyncIterator[dict]:
    """给 SSE 事件流兜底:中途异常若直接冒出,连接会静默断开 —— 前端只拿到半截答案却毫无提示。

    这里补发一条 error 事件,让前端能明说「回答中断」。CancelledError 继承自 BaseException,
    客户端主动断开(或前端点「停止」)时不会被这里吞掉,仍按取消处理。
    """
    try:
        async for item in agen:
            yield item
    except Exception:  # noqa: BLE001 —— 任何异常都要让前端知道,不能无声收场
        logger.exception("流式回答中断(thread_id=%s)", thread_id)
        msg = "回答中断:后端处理出错,请重试。"
        yield {"event": "error", "data": json.dumps({"message": msg}, ensure_ascii=False)}


@router.post("/chat/stream", dependencies=[Depends(require_user)])
async def chat_stream(req: ChatRequest, request: Request,
                      principal: Principal = Depends(require_user)) -> EventSourceResponse:
    """SSE 流式:先发 meta(带 thread_id),随后 token/tool_call/tool_result 均带 step(agent 轮次号)+ done(附 skill)。

    step 从 0 起,每当 tools 节点跑完 +1 —— 下一轮 agent 的输出属于新的 step。前端据此
    把扁平事件流切成时间线:某 step 跟了 tool_call 即为思考段,一直没有 tool_call 的最后
    一段即为最终答案。后端不再判定「思考 vs 答案」,分类交给前端(见 useChat.ts)。
    """
    thread_id = await _resolve_thread(req, principal)
    config = {"configurable": {"thread_id": thread_id,
                               "todos_prompt": await _todos_prompt(request),
                               "memory_prompt": await _memory_prompt(principal, req.message)}}
    inputs = {"messages": [HumanMessage(content=req.message)]}
    graph = _graph(request)

    async def event_gen():
        # 先把本通对话的 thread_id 交给前端存下,后续提问回传即可续接记忆。
        yield {"event": "meta", "data": json.dumps({"thread_id": thread_id}, ensure_ascii=False)}
        seen: list = []  # 累积 agent + tools 产出的消息,done 时回读本次引用的 SOP 与知识库来源
        final = ""  # 最后一段**没有工具调用**的 agent 输出 = 本次的完整回答(登记提取用)
        step = 0  # agent 轮次号:token / tool_call / 该轮 tool_result 共享它
        # 登记在途生成(整个生成期间,含客户端中途断开):删除会话要先等它收尾。
        async with inflight.track(thread_id):
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
                            calls = getattr(msg, "tool_calls", None) or []
                            for call in calls:
                                yield {"event": "tool_call", "data": json.dumps({"name": call.get("name"), "args": call.get("args") or dict(), "step": step}, ensure_ascii=False)}
                            if not calls:            # 没有工具调用 = 这段就是最终回答
                                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                                if text.strip():
                                    final = text
                    elif node == "tools":
                        seen.extend(messages)  # ToolMessage 带 artifact → done 时供 used_sources 读知识库来源
                        for msg in messages:
                            yield {"event": "tool_result", "data": json.dumps({"name": getattr(msg, "name", None), "tool_call_id": getattr(msg, "tool_call_id", None), "step": step}, ensure_ascii=False)}
                        # 工具跑完 → 下一轮 agent 属于新的 step。
                        step += 1
        # 事件流跑到这里 = 生成完整结束(中途断开 / 抛错都不会执行到这里):
        # 登记提取任务(**在 done 之前** —— 前端收到 done 就可能关连接,留到后面可能登记不上)。
        await _remember_exchange(principal, thread_id, req.message, final, source="chat_stream")
        sources = [s.model_dump() for s in _sign_sources(used_sources(seen))]
        yield {"event": "done", "data": json.dumps({"skill": used_sop_id(seen), "sources": sources, "images": used_images(seen)}, ensure_ascii=False)}

    return EventSourceResponse(_guarded(event_gen(), thread_id))


# 挂在 /admin 前缀下的路由都要过 auth_ready(与 admin_users.py 一致:
# 配置缺失时明确 503,而不是让管理员以为是自己密码错了)。
@router.post("/admin/sops/reload",
             dependencies=[Depends(auth_ready), Depends(require_admin)])
async def reload_sops() -> dict:
    """热重载 sops/ 目录,返回当前 Skill 数量。"""
    return {"reloaded": loader.reload()}
