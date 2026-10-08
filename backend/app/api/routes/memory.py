"""「我的记忆」接口(前缀 /api/v1/memory,§12):查看 / 检索 / 编辑 / 删除 / 彻底清除。

权限与归属(这一层的全部规矩):
- 所有接口都要登录(require_user),**user_id 只来自 Principal** —— 请求体里没有、也不接受
  任何用户标识,前端无从指定归属;
- 按 id 的读 / 改 / 删 / 历史一律走 service,由它按 (user_id, memory_id) 校验归属;
  别人的 id 与不存在的 id 返回同样的 404(不泄露「这条存在」);
- 管理员在这个前缀下**没有特殊权限**:私人记忆不是账目,不因为角色就敞开展示
  (要排查请走运维手段,不在这里开后门)。

开关(§12:必须区分「暂停写入」与「暂停检索」,且关闭不删数据):
- MEMORY_ENABLED=false:整个功能关闭 —— 本组接口 503 并说明,聊天照常且不带记忆;
- MEMORY_WRITE_ENABLED=false:暂停**自动**写入(不再登记 / 执行提取任务);
  用户自己在这页里的手添 / 编辑 / 删除不受影响 —— 那是显式意图,没有理由拦;
- MEMORY_SEARCH_ENABLED=false:暂停**回答时**的记忆检索;本页的检索是用户主动发起的,
  照常工作。状态接口把三个开关都照实报出来,前端原样展示。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.deps import auth_ready, require_user
from app.auth.service import Principal
from app.config import get_settings
from app.memory import db, service
from app.memory.errors import (
    MemoryConflict, MemoryDisabled, MemoryNotFound, MemoryNotConfigured, MemoryStaleGeneration,
)
from app.schemas.memory import (
    MemoryAddRequest, MemoryClearResponse, MemoryDeleteResponse, MemoryEditRequest,
    MemoryHistoryItem, MemoryHistoryResponse, MemoryItemView, MemoryListResponse,
    MemoryReindexResponse, MemoryStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/memory", tags=["memory"],
                   dependencies=[Depends(auth_ready), Depends(require_user)])

MAX_PAGE_SIZE = 200


def _require_usable() -> None:
    """没配库 / 功能被关掉时明确报 503,而不是假装「你没有记忆」。"""
    if not db.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="长期记忆未配置(MYSQL_URL 为空):已存记忆不受影响,配置后重启即可使用",
        )
    if not get_settings().memory_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("长期记忆已关闭(MEMORY_ENABLED=false):本次不读也不写记忆;"
                    "已存数据仍在库里,打开开关即可恢复"),
        )


@contextmanager
def _translated():
    """把记忆层的异常翻译成 HTTP 状态码;**不把异常原文回给前端**。

    外来异常(SQLAlchemy / httpx…)的 str() 可能带着 SQL 参数 —— 也就是记忆正文,
    所以这里只回类名,细节留在服务端日志(§7 不落敏感正文)。
    """
    try:
        yield
    except HTTPException:
        raise
    except MemoryNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc)) from exc
    except MemoryDisabled as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"长期记忆不可用:{exc}") from exc
    except MemoryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="这条记忆不存在(或不属于你)") from exc
    except MemoryStaleGeneration as exc:
        # 用户中途「彻底删除」过:本次写入作废 —— 409 让前端提示「记忆已被清除,请刷新」
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MemoryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 统一翻译成可读但不含正文的 503
        logger.warning("记忆接口失败(%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"记忆服务暂时不可用({type(exc).__name__}),请稍后重试",
        ) from exc


def _view(item) -> MemoryItemView:
    """记忆行 → 接口视图(索引是否同步按**当前** Embedding 版本现算)。"""
    return MemoryItemView(
        id=item.id, text=item.text, status=item.status, origin=item.origin,
        revision=item.revision, thread_id=item.thread_id, created_at=item.created_at,
        updated_at=item.updated_at,
        index_state=("synced" if item.embedding_version == service.embedding_version()
                     else "pending"),
        indexed_at=item.indexed_at,
    )


@router.get("", response_model=MemoryListResponse, summary="我的记忆(列表 / 检索)")
def list_memory(
    q: str = Query(default="", max_length=200, description="检索词;留空 = 按更新时间倒序的普通列表"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移(普通列表分页用)"),
    principal: Principal = Depends(require_user),
) -> MemoryListResponse:
    """列出 / 检索**自己的**记忆。

    带 `q` 时走混合检索(向量 + 中文 BM25 → RRF → 重排序),相关性排序;
    不带时直接分页 —— 列表不需要相关性排序,也就不必为它花一次向量化调用。
    """
    _require_usable()
    with _translated():
        result = service.list_items(user_id=principal.user_id, query=q, limit=limit, offset=offset)
    return MemoryListResponse(
        enabled=True,
        items=[_view(i) for i in result["items"]],
        total=int(result["total"]),
        index_pending=service.index_pending_count(user_id=principal.user_id),
        query=result["query"], degraded=list(result["degraded"]), error=str(result["error"]),
    )


@router.post("", response_model=MemoryItemView, status_code=status.HTTP_201_CREATED,
             summary="手添一条记忆")
def add_memory(body: MemoryAddRequest,
               principal: Principal = Depends(require_user)) -> MemoryItemView:
    """自己写一条记忆(来源记为 user)。与已有记忆正文重复时 409,不写重复行。"""
    _require_usable()
    with _translated():
        item = service.add_item(user_id=principal.user_id, text=body.text)
    return _view(item)


@router.patch("/{memory_id}", response_model=MemoryItemView, summary="编辑一条记忆")
def edit_memory(memory_id: str, body: MemoryEditRequest,
                principal: Principal = Depends(require_user)) -> MemoryItemView:
    """改写自己的一条记忆(版本在读取与写入之间被别人改过则 409,不覆盖)。"""
    _require_usable()
    with _translated():
        item = service.edit_item(user_id=principal.user_id, memory_id=memory_id, text=body.text)
    return _view(item)


@router.delete("/{memory_id}", response_model=MemoryDeleteResponse, summary="删除一条记忆")
def delete_memory(memory_id: str,
                  principal: Principal = Depends(require_user)) -> MemoryDeleteResponse:
    """删掉自己的一条记忆:立即不再参与检索,同时清掉它的向量(行保留供审计)。

    **不等于删聊天记录**:那次对话仍在会话里,只是这条事实不再被记住。

    返回里带 `cleanup`:事实已经删掉,但索引侧的向量点可能还没清完(那就登记了台账,
    后台按退避重试)——界面据此显示「正在清理 / 清理失败」,不笼统报「删除完成」。
    """
    _require_usable()
    with _translated():
        result = service.delete_item(user_id=principal.user_id, memory_id=memory_id)
    return MemoryDeleteResponse(id=result["item"].id, cleanup=result["cleanup"],
                                degraded=([] if result["vector_cleaned"] else
                                          [service.VECTOR_CLEANUP_PENDING_NOTE]))


@router.get("/history", response_model=MemoryHistoryResponse, summary="变更历史")
def memory_history(
    memory_id: str = Query(default="", max_length=64, description="只看某条记忆的变更;留空 = 全部"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移"),
    principal: Principal = Depends(require_user),
) -> MemoryHistoryResponse:
    """自己记忆的变更审计(新的在前)。彻底清除后正文列已脱敏为 null。"""
    _require_usable()
    with _translated():
        rows = service.history(user_id=principal.user_id, memory_id=memory_id or None,
                               limit=limit, offset=offset)
    return MemoryHistoryResponse(enabled=True, items=[
        MemoryHistoryItem(id=r.id, memory_id=r.memory_id, event=r.event, old_text=r.old_text,
                          new_text=r.new_text, actor=r.actor, reason=r.reason,
                          created_at=r.created_at)
        for r in rows
    ])


@router.get("/status", response_model=MemoryStatusResponse, summary="功能状态")
def memory_status(principal: Principal = Depends(require_user)) -> MemoryStatusResponse:
    """开关与规模:哪些开关是关的、有多少条、有多少条待补索引、后台任务是否卡住。

    功能整体关闭时也照常返回(状态本身要看得见),只是各项数字为 0。
    """
    with _translated():
        stats = service.status(user_id=principal.user_id)
    return MemoryStatusResponse(**stats)


@router.post("/reindex", response_model=MemoryReindexResponse, summary="补建索引")
def reindex(principal: Principal = Depends(require_user),
            limit: int = Query(default=200, ge=1, le=1000,
                               description="本次最多补写多少条")) -> MemoryReindexResponse:
    """把自己「待索引」的记忆补写进 Qdrant(索引写失败 / 换 Embedding 模型之后用)。

    只动派生索引:失败也不影响记忆本身,返回真实的 requested / indexed / deferred。
    """
    _require_usable()
    with _translated():
        result = service.ensure_indexed(user_id=principal.user_id, limit=limit)
    return MemoryReindexResponse(requested=result.requested, indexed=result.indexed,
                                 payload_only=result.payload_only,
                                 deferred=result.deferred, error=result.error)


@router.delete("", response_model=MemoryClearResponse, summary="彻底删除我的长期记忆")
def clear_memory(confirm: bool = Query(default=False, description="必须显式确认为 true"),
                 principal: Principal = Depends(require_user)) -> MemoryClearResponse:
    """彻底删除自己的长期记忆(需 `?confirm=true`)。

    删掉:全部记忆、待执行的任务(含任务里暂存的对话正文);审计行保留但正文脱敏。
    **不等于删除账号与聊天记录** —— 对话仍在,之后的交流还会重新提取出同样的事实;
    清除这件事本身在返回结果里逐项如实汇报(含向量没清干净的情况)。
    """
    _require_usable()
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("需要显式确认(?confirm=true):这会删除你的全部长期记忆与待执行的提取任务;"
                    "审计行保留但正文脱敏,聊天记录与账号不受影响,清除后仍可正常使用"),
        )
    with _translated():
        result = service.clear_user(user_id=principal.user_id)
    return MemoryClearResponse(**result)
