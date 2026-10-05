"""健康检查:唯一不需要登录的探针,也是唯一允许匿名看到的东西。

刻意只回一个词:**不**报告版本号、进程数、依赖连通性、配置来源 ——
那些是给运维看的,一旦匿名可见就是给扫描器的礼物(哪些服务在跑、用了什么库)。
需要更细的自检请用受保护的管理接口或服务器上的命令行工具。
"""

from fastapi import APIRouter, Depends

from app.auth.deps import public_endpoint

router = APIRouter(tags=["health"], dependencies=[Depends(public_endpoint)])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
