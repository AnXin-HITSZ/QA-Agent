"""Evaluation-only billing stop; never logs provider bodies or credentials."""
import json
from datetime import datetime, timezone
from pathlib import Path


def exhausted(status, payload):
    if status == 402:
        return True
    if status < 400:
        return False
    text = json.dumps(payload, ensure_ascii=False).casefold()
    return any(term in text for term in (
        "insufficient_balance", "insufficient balance", "insufficientbalance", "balance.insufficient",
        "insufficient_quota", "quotaexhausted", "quota exhausted", "allocationquota", "arrearage",
        "余额不足", "欠费", "额度耗尽", "free tier exhausted"))


def install(marker: Path):
    """SystemExit bypasses retrieval degradation and SDK retry handlers."""
    import httpx
    original = httpx.Client.send
    if getattr(original, "_eval_billing_guard", False):
        return

    def send(client, request, *args, **kwargs):
        response = original(client, request, *args, **kwargs)
        if response.status_code >= 400:
            response.read()
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            if exhausted(response.status_code, payload):
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({"stopped_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "provider_billing_or_quota_exhausted", "http_status": response.status_code}),
                    encoding="utf-8")
                response.close()
                raise SystemExit("供应商余额或额度不足，已停止本进程所有付费调用；进度已保留。")
        return response

    send._eval_billing_guard = True
    httpx.Client.send = send
