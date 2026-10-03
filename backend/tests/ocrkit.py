"""构造阿里云 RecognizeAllText 形态的假响应(测试离线跑 OCR)。

打桩点是 ocr.recognize_raw —— 真实网络边界的最内层:两层缓存、归一化、逐页编排
仍然走真代码,所以「命中缓存不再调用供应商」这类断言才是真断言。
"""

from __future__ import annotations

from typing import Any


def raw_response(text: str = "识别文本", fields: dict | None = None, *,
                 request_id: str = "req-test", subimage_count: int | None = None,
                 is_mixed_mode: bool = False) -> dict:
    """一份最小可用的供应商响应:整页文本在 Data.Content,票据字段在 SubImages[].KvInfo.Data。

    字段给定时(票据 / 付款详情类)normalize 会优先用结构化字段拼正文,与真实响应一致。
    """
    data: dict[str, Any] = {
        "Content": text,
        "SubImageCount": subimage_count if subimage_count is not None else (1 if fields else 0),
        "IsMixedMode": is_mixed_mode,
    }
    if fields:
        data["SubImages"] = [{"KvInfo": {"Data": dict(fields)}}]
    return {"RequestId": request_id, "Data": data}


def install(monkeypatch, *, text: str = "识别文本", fields: dict | None = None,
            calls: list | None = None, boom: Exception | None = None,
            request_id: str = "req-test", is_mixed_mode: bool = False):
    """把 ocr.recognize_raw 换成假实现;同一测试内可多次调用换行为(monkeypatch 逐层还原)。

    calls 传入列表时,每次调用记 (图片字节数, ocr_type),便于断言调用次数与类型。
    boom 传入异常时抛它,模拟供应商失败(失败不写缓存)。
    """
    from app.rag import ocr

    def _fake(image: bytes, ocr_type: str, **kw) -> dict:
        if calls is not None:
            calls.append((len(image), ocr_type))
        if boom is not None:
            raise boom
        return raw_response(text, fields, request_id=request_id, is_mixed_mode=is_mixed_mode)

    monkeypatch.setattr(ocr, "recognize_raw", _fake)
    return _fake
