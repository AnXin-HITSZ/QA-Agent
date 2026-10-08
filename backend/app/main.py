from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    admin_users, auth, chat, conversations, health, knowledge, memory, metering, sops, todos,
)
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

    # 认证服务:用户 / 会话 / 会话目录 / 审计都在 MySQL(MYSQL_URL 指向的库)。
    # 这里只是装配对象,不建连接、不校验配置 —— 真正的「没配就明确报错」发生在请求路径上
    # (app/auth/db.py 的 _require_configured → 503,绝不降级为匿名可用)。
    from app.auth.service import AuthService

    app.state.auth = AuthService()
    from app.auth import db as auth_db

    if not auth_db.configured():
        logger.warning("认证:未配置 MYSQL_URL —— 认证 / 用户管理接口将返回 503;"
                       "聊天等公开能力不受影响。见 docs/认证鉴权与用户管理技术方案.md §10。")

    # 待办清单存储:普通 Redis(单键 GET/SET),不依赖 RedisJSON/RediSearch,与 checkpointer
    # 各自独立 —— 即便跨轮记忆因缺模块降级,待办仍可用;失败置 None,接口层再优雅降级。
    todo_store = None
    if settings.redis_url:
        try:
            from app.todos import create_todo_store

            todo_store = await create_todo_store(settings.redis_url)
            logger.info("待办清单:Redis 存储已启用")
        except Exception as exc:
            logger.warning("待办清单:Redis 初始化失败(%s),待办特性降级为不可用;不影响聊天与 RAG。", exc)
            todo_store = None
    app.state.todos = todo_store

    # 索引任务对账:上次进程留下的 queued / running 任务不可能自己复活,标记为中断失败,
    # 让前端看到明确状态(已识别页面都在 OCR 缓存里,重新排队不会重复付费)。
    try:
        from app.rag import ocr_jobs

        ocr_jobs.reconcile()
    except Exception as exc:
        logger.warning("索引任务对账失败(不影响启动):%s", exc)

    # 删除会话对账:上次进程删了一半(卡在 deleting)的会话在这里删完 —— 用户点过删除的
    # 正文不该因为一次重启就一直留在 Redis 里。Redis 不可用 / 库未配就跳过,不影响启动。
    if checkpointer is not None:
        try:
            from app.conversations import reconcile_deleting

            done = await reconcile_deleting(checkpointer)
            if done:
                logger.info("会话删除对账:补完 %d 条", done)
        except Exception as exc:
            logger.warning("会话删除对账失败(不影响启动):%s", exc)

    # 调用日志与费用统计:起后台补写线程(不建表、不阻塞启动;未配 MYSQL_URL 则什么都不做,
    # 索引与检索照常)。表结构由迁移脚本建立,见 docs/调用日志与费用统计技术方案.md §6。
    try:
        from app.metering import start as metering_start, status as metering_status

        metering_start()
        st = metering_status()
        if st.get("enabled"):
            logger.info("调用日志:已启用(MySQL %s)", "连接正常" if st.get("db_ok") else "连接待确认")
        else:
            logger.info("调用日志:%s", st.get("message") or "未启用")
    except Exception as exc:
        logger.warning("调用日志初始化失败(不影响启动与业务):%s", exc)

    # 长期记忆:起后台提取线程(认领 MySQL 里的记忆任务 → 提取 → 维护落库)。
    # 未配 MYSQL_URL / 显式关闭 / 起线程失败都不影响启动与聊天 —— 记忆是软依赖。
    try:
        from app.memory import worker as memory_worker

        memory_worker.start()
        st = memory_worker.status()
        if st.get("enabled") and st.get("configured"):
            logger.info("长期记忆:后台提取线程已启动;任务队列 %s", st.get("jobs") or "空")
        else:
            logger.info("长期记忆:未启用(见配置 MEMORY_ENABLED / MYSQL_URL)")
    except Exception as exc:
        logger.warning("长期记忆初始化失败(不影响启动与聊天):%s", exc)

    try:
        yield
    finally:
        try:
            from app.auth import ratelimit

            await ratelimit.close()          # 限流 Redis 连接:不关会拖着 aiohttp 任务不放
        except Exception as exc:
            logger.warning("限流连接收尾失败:%s", exc)
        try:
            from app.memory import worker as memory_worker

            memory_worker.stop()
        except Exception as exc:
            logger.warning("长期记忆线程收尾失败:%s", exc)
        try:
            from app.metering import stop as metering_stop

            metering_stop()
        except Exception as exc:
            logger.warning("调用日志收尾失败:%s", exc)
        if saver_cm is not None:
            await saver_cm.__aexit__(None, None, None)
        if todo_store is not None:
            await todo_store.close()


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

    # 认证异常 → 统一错误体(401/403/404/409/429/503)。一次装好,所有路由共用。
    from app.auth import http as auth_http

    auth_http.install(app)

    app.include_router(health.router)
    app.include_router(auth.router)               # 注册 / 登录 / 刷新 / 设备管理
    app.include_router(admin_users.router)        # 管理员:用户审批与审计
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(knowledge.router)
    app.include_router(knowledge.admin_router)
    app.include_router(memory.router)            # 我的记忆(普通用户自管理,user_id 只来自 Principal)
    app.include_router(metering.router)          # 调用日志与费用统计(admin,见 §9 权限说明)
    app.include_router(sops.router)
    app.include_router(todos.router)
    return app


app = create_app()
