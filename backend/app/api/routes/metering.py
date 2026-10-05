"""调用日志与费用统计接口(前缀 /api/v1/admin/metering)。

三条口径(与前端、技术方案一致):
- **日志只读、价目只增可删**:日志由业务链路写入,接口不提供改 / 删,也不提供「清空历史」;
  价目表只支持新增与删除误录条目 —— 改价 = 追加一条更晚生效的规则,历史事件按发生时的
  快照估算,绝不重算;
- **估算**:所有金额都标 cost_status=estimated 且原因是「按配置单价 × 用量估算」;
  缺用量 / 缺价格时金额为空并给出原因,绝不返回 0;
- **不合并**:不同币种、不同用量单位分开返回,不给跨币种 / 跨单位合计。

权限:路径在 /admin/ 下,**仅管理员**(require_admin)—— 费用是全局账,普通用户看不到
(见 docs/认证鉴权与用户管理技术方案.md §6 权限矩阵)。前端隐藏入口只是界面礼貌,真正的门
在这个依赖上;认证配置缺失时 auth_ready 先返回 503,而不是放行。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.deps import auth_ready, require_admin
from app.config import get_settings
from app.metering import METERING_DISABLED_MESSAGE, db, get_writer, status as metering_status
from app.metering.redact import safe_text
from app.metering.store import CallFilter, MAX_PAGE_SIZE, PriceExists, get_store
from app.schemas.metering import (
    SERVICES, CallLogItem, CallLogPage, MeteringSummary, PriceRuleInput, PriceRuleList, PriceRuleView,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/admin/metering", tags=["metering"],
                   dependencies=[Depends(auth_ready), Depends(require_admin)])

STATUSES = ("success", "failure")
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366


@contextmanager
def _db_503():
    """数据库不可用 / 未配置 → 503(前端据此提示,而不是当成「没有数据」)。"""
    try:
        yield
    except HTTPException:
        raise                     # 已翻译好的业务错误(404 / 409)原样透传,不吞成 503
    except Exception as exc:  # noqa: BLE001 —— 统一翻译成可读、已脱敏的 503
        logger.warning("调用日志查询失败:%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="调用日志数据库不可用:" + safe_text(f"{type(exc).__name__}: {exc}", limit=200),
        ) from exc


def _parse_time(value: str | None, *, what: str) -> datetime | None:
    """ISO8601(可带偏移)或 epoch 秒 → UTC datetime;非法值 400(不猜时区)。"""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        if raw.lstrip("-").isdigit():                 # epoch 秒
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{what} 不是合法时间:{safe_text(raw, limit=40)}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    """默认最近 7 天(前端首屏不拉全量历史);时间窗上限 366 天,避免误查全表。"""
    end = _parse_time(until, what="until") or datetime.now(timezone.utc)
    start = _parse_time(since, what="since") or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    if start >= end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="since 必须早于 until")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"时间窗最长 {MAX_WINDOW_DAYS} 天,请缩小范围")
    return start, end


def _filters(*, since: str | None, until: str | None, service: str | None = None,
             call_status: str | None = None, purpose: str | None = None,
             job_id: str | None = None, document_id: str | None = None) -> CallFilter:
    if service and service not in SERVICES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"未知 service={safe_text(service, limit=20)}(可选:{'/'.join(SERVICES)})")
    if call_status and call_status not in STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"未知 status={safe_text(call_status, limit=20)}(可选:{'/'.join(STATUSES)})")
    start, end = _window(since, until)
    return CallFilter(since=start, until=end, service=service, status=call_status, purpose=purpose,
                      job_id=job_id or None, document_id=document_id or None)


@router.get("/calls", response_model=CallLogPage, summary="调用日志(分页 + 过滤)")
def list_calls(
    since: str | None = Query(default=None, description="起始时间(ISO8601 或 epoch 秒;缺省 = 最近 7 天)"),
    until: str | None = Query(default=None, description="结束时间(不含;缺省 = 现在)"),
    service: str | None = Query(default=None, description="embedding / ocr"),
    call_status: str | None = Query(default=None, alias="status", description="success / failure"),
    purpose: str | None = Query(default=None, description="document_index / query"),
    job_id: str | None = Query(default=None, description="索引任务 id"),
    document_id: str | None = Query(default=None, description="文件身份 id"),
    offset: int = Query(default=0, ge=0, description="偏移"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数(≤{MAX_PAGE_SIZE})"),
) -> dict:
    """按时间倒序返回调用记录;只返回当前页,不做全量导出。"""
    flt = _filters(since=since, until=until, service=service, call_status=call_status,
                   purpose=purpose, job_id=job_id, document_id=document_id)
    with _db_503():
        return get_store().list_calls(flt, offset=offset, limit=limit)


@router.get("/calls/{event_id}", response_model=CallLogItem, summary="调用详情")
def get_call(event_id: str) -> dict:
    """单条调用详情:含按文件 / 页面的估算分摊(items)与当时的价目快照。"""
    eid = (event_id or "").strip()
    if not eid or len(eid) > 64:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="事件 id 不合法")
    with _db_503():
        row = get_store().get_call(eid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到该调用记录")
    return row


@router.get("/summary", response_model=MeteringSummary, summary="概览统计")
def summary(
    since: str | None = Query(default=None, description="起始时间(ISO8601 或 epoch 秒;缺省 = 最近 7 天)"),
    until: str | None = Query(default=None, description="结束时间(不含;缺省 = 现在)"),
    service: str | None = Query(default=None, description="embedding / ocr"),
    purpose: str | None = Query(default=None, description="document_index / query"),
    job_id: str | None = Query(default=None, description="索引任务 id"),
    document_id: str | None = Query(default=None, description="文件身份 id"),
) -> dict:
    """概览:实际调用 / 成功 / 失败 / 用量(按单位)/ 估算费用(按币种)/ 缓存命中 / 持久化健康。

    不同币种、不同用量单位分开返回;数据库不可用时仍返回 200,但 persistence 里如实说明,
    避免「查不到」被误当成「没有调用」。
    """
    flt = _filters(since=since, until=until, service=service, purpose=purpose, job_id=job_id,
                   document_id=document_id)
    out: dict = {"since": flt.since.isoformat() if flt.since else None,
                 "until": flt.until.isoformat() if flt.until else None,
                 "filters": {"service": flt.service, "purpose": flt.purpose,
                             "job_id": flt.job_id, "document_id": flt.document_id}}
    try:
        out.update(get_store().summary(flt))
    except Exception as exc:  # noqa: BLE001 —— 数据库故障:概览给空,健康状态里说明
        logger.warning("调用概览统计失败:%s", exc)
        out["error"] = "统计失败:" + safe_text(f"{type(exc).__name__}: {exc}", limit=200)
    try:
        out["persistence"] = metering_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取调用日志健康状况失败:%s", exc)
        out["persistence"] = {"enabled": False, "configured": db.configured(),
                              "message": METERING_DISABLED_MESSAGE}
    return out


@router.get("/prices", response_model=PriceRuleList, summary="当前价目表")
def prices() -> dict:
    """价目表:估算费用的依据;为空说明还没配价格(界面显示「无法估算」,绝不显示 0)。"""
    with _db_503():
        return {"items": get_store().price_rules()}


def _price_row(payload: PriceRuleInput) -> dict:
    """请求体 → 存储行:生效时间必须能解析成带时区的时刻(缺省按 UTC,不猜本地时区)。"""
    eff = _parse_time(payload.effective_from, what="effective_from")
    if eff is None:  # schema 已要求非空,这里只兜底(正常到不了)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="effective_from 不能为空")
    return {"service": payload.service, "provider": payload.provider, "target": payload.target,
            "unit": payload.unit, "currency": payload.currency, "unit_price": payload.unit_price,
            "effective_from": eff, "source": payload.source, "note": payload.note}


def _refresh_prices() -> None:
    """写入价目后立刻作废价格表缓存(不让新价格白等一个缓存周期);刷新失败不掩盖写入成功。"""
    try:
        get_writer().price_book.invalidate()
    except Exception:  # noqa: BLE001 —— 最多滞后一个缓存周期生效,不影响已写入的事实
        logger.warning("价目写入后刷新价格表缓存失败(新价目将滞后生效)", exc_info=True)


@router.post("/prices", response_model=PriceRuleView, status_code=status.HTTP_201_CREATED,
             summary="新增价目(只增)")
def create_price(payload: PriceRuleInput) -> dict:
    """新增一条价目。

    **只增不改**:同一(服务/供应商/模型/单位/币种/生效时间)重复录入返回 409;
    改价 = 追加一条生效时间更晚的规则 —— 历史事件用的是发生时刻的价目快照,不会重算。
    """
    row = _price_row(payload)
    with _db_503():
        try:
            item = get_store().insert_price(row)
        except PriceExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="同一(服务/供应商/模型/单位/币种/生效时间)的价目已存在;"
                       "改价请把生效时间改到更晚,而不是重复录入同一条。",
            ) from exc
        _refresh_prices()
    return item


@router.delete("/prices/{price_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除价目")
def delete_price(price_id: int) -> None:
    """删除一条误录的价目。

    历史事件保留发生时刻的价目快照与估算金额,删除只影响之后的调用;对账时以快照为准。
    """
    with _db_503():
        if not get_store().delete_price(int(price_id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="未找到该价目(可能已被删除)")
        _refresh_prices()
