from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, conversations, health
from app.config import get_settings
from app.graph import build_graph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时装配对话图并存到 app.state.graph。

    配了 REDIS_URL 就挂 AsyncRedisSaver(跨轮对话记忆),checkpointer 全程存活、关机时释放;
    初始化失败(URL 错 / 缺 RedisJSON+RediSearch 模块 / 安全组没放行)则打告警、退化为单轮模式,
    保证后端照常起来(RAG、SOP 不受影响)。
    """
    settings = get_settings()
    saver_cm = None
    checkpointer = None
    if settings.redis_url:
        try:
            from langgraph.checkpoint.redis import AsyncRedisSaver

            ttl = {"default_ttl": settings.redis_ttl_minutes, "refresh_on_read": True} if settings.redis_ttl_minutes > 0 else None
            # 连接加固:socket_keepalive 让空闲连接不被公网 NAT/空闲超时悄悄掐断,
            # health_check_interval 在复用前先探活、剔除死连接。主要为 Windows 开发机:
            # 那里 uvicorn 无 uvloop、回退到 asyncio SelectorEventLoop,Py3.12 有个 writelines
            # bug——复用到死连接会崩成 TypeError 而非可重试的 ConnectionError(生产 Linux 走
            # uvloop 无此坑)。透传进 AsyncRedis.from_url,对生产纯属无害加固。
            conn_args = {"socket_keepalive": True, "health_check_interval": 30}
            saver_cm = AsyncRedisSaver.from_conn_string(settings.redis_url, ttl=ttl, connection_args=conn_args)
            checkpointer = await saver_cm.__aenter__()
            await checkpointer.asetup()  # 首次连接建 RedisJSON/RediSearch 索引;已存在则幂等跳过
            logger.info("对话记忆:Redis checkpointer 已启用(跨轮记忆开)")
        except Exception as exc:
            logger.warning("对话记忆:Redis 初始化失败(%s),退化为单轮模式;请检查 REDIS_URL / RedisJSON+RediSearch 模块 / 安全组。", exc)
            if saver_cm is not None:
                try:
                    await saver_cm.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
            saver_cm = None
            checkpointer = None
    app.state.graph = build_graph(checkpointer=checkpointer)
    # 同一个 checkpointer 也给历史对话路由用(扫描 / 读取 / 删除);降级时为 None。
    app.state.checkpointer = checkpointer
    if checkpointer is None:
        logger.info("对话记忆:单轮模式(无跨轮记忆);配置 REDIS_URL 即可开启。")
    try:
        yield
    finally:
        if saver_cm is not None:
            await saver_cm.__aexit__(None, None, None)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Lab QA Assistant", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    return app


app = create_app()
