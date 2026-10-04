"""调用日志与费用统计接口(管理员,前缀 /api/v1/admin/metering)。

三条口径(与前端、技术方案一致):
- **只读**:日志由业务链路写入,接口不提供改 / 删;也不提供「清空历史」;
- **估算**:所有金额都标 cost_status=estimated 且原因是「按配置单价 × 用量估算」;
  缺用量 / 缺价格时金额为空并给出原因,绝不返回 0;
- **不合并**:不同币种、不同用量单位分开返回,不给跨币种 / 跨单位合计。

权限:沿用现有 admin 前缀;后端目前**没有**成体系的登录态(见技术方案 §9),
因此这里提供 ADMIN_API_TOKEN + X-Admin-Token 作为可选的第一道门:
配置了就必须带对,没配置则与其它 admin 接口一样敞开 —— 此时状态接口会明确
返回 auth_configured=false,前端据此显示警示,避免把费用与文件日志当成已受保护。
"""

from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.config import get_settings
from app.metering import METERING_DISABLED_MESSAGE, db, status as metering_status
from app.metering.redact import safe_text
from app.metering.store import CallFilter, MAX_PAGE_SIZE, get_store
from app.schemas.metering import CallLogItem, CallLogPage, MeteringSummary, PriceRuleList

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix + "/admin/metering", tags=["metering"])

SERVICES = ("embedding", "ocr")
STATUSES = ("success", "failure")
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """可选的令牌校验:配了 ADMIN_API_TOKEN 就必须带对;没配则不拦(并在状态里标注)。"""
    expected = (get_settings().admin_api_token or "").strip()
    if not expected:
        return
    got = (x_admin_token or "").strip()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="管理员令牌无效或缺失(请在请求头带 X-Admin-Token)")


@contextmanager
def _db_503():
    """数据库不可用 / 未配置 → 503(前端据此提示,而不是当成「没有数据」)。"""
    try:
        yield
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
    _: None = Depends(require_admin),
) -> dict:
    """按时间倒序返回调用记录;只返回当前页,不做全量导出。"""
    flt = _filters(since=since, until=until, service=service, call_status=call_status,
                   purpose=purpose, job_id=job_id, document_id=document_id)
    with _db_503():
        return get_store().list_calls(flt, offset=offset, limit=limit)


@router.get("/calls/{event_id}", response_model=CallLogItem, summary="调用详情")
def get_call(event_id: str, _: None = Depends(require_admin)) -> dict:
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
    _: None = Depends(require_admin),
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
def prices(_: None = Depends(require_admin)) -> dict:
    """价目表(运维在 MySQL 里配置):估算费用的依据;为空说明还没配价格。"""
    with _db_503():
        return {"items": get_store().price_rules()}
