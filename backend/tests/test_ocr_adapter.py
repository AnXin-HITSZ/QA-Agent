"""OCR 适配器:签名 / 归一化 / 重试 / 错误分类。

全部离线:正常路径把 ocr._post_once 换成给定响应,不产生任何真实调用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from app.rag import ocr


# ---- 签名 ----

def test_sign_v1_matches_rpc_style_algorithm():
    """独立按官方算法重算一遍:排序 → 规范化 → POST&%2F&… → HMAC-SHA1 → base64。"""
    params = {
        "Action": "RecognizeAllText", "Version": "2021-07-07", "Format": "JSON",
        "AccessKeyId": "ak", "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "SignatureNonce": "nonce", "Timestamp": "2026-10-03T00:00:00Z", "Type": "Invoice",
    }
    canon = "&".join(f"{ocr._pct(k)}={ocr._pct(v)}" for k, v in sorted(params.items()))
    sts = f"POST&{ocr._pct('/')}&{ocr._pct(canon)}"
    expect = base64.b64encode(
        hmac.new(b"sk&", sts.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    assert ocr._sign_v1(params, "sk") == expect


def test_pct_escapes_rfc3986():
    assert ocr._pct("a b/c") == "a%20b%2Fc"
    assert ocr._pct("A-Z_a.b~c") == "A-Z_a.b~c"


# ---- 归一化 ----

def _sub(kv: dict | None = None, blocks: list[str] | None = None) -> dict:
    sub: dict = {"Type": "Invoice"}
    if kv is not None:
        sub["KvInfo"] = {"Data": kv}
    if blocks:
        sub["BlockInfo"] = {"BlockDetails": [{"BlockContent": b} for b in blocks]}
    return sub


def test_normalize_general_prefers_content():
    raw = {"RequestId": "r1", "Data": {"Content": "整页文字", "SubImages": [_sub(blocks=["块1"])]}}
    page = ocr.normalize(raw, "general")
    assert page.text == "整页文字"
    assert page.aliyun_type == "Advanced"
    assert page.request_id == "r1"


def test_normalize_invoice_builds_field_lines_and_marks_fields():
    raw = {"Data": {"Content": "整页文字整页文字整页文字", "SubImages": [_sub(
        kv={"发票号码": "12345678", "价税合计": "¥100.00", "备注": ""},
        blocks=["无关块"],
    )]}}
    page = ocr.normalize(raw, "invoice")
    assert page.text == "发票号码: 12345678\n价税合计: ¥100.00"   # 空字段不塞、原文保留
    assert page.fields == {"发票号码": "12345678", "价税合计": "¥100.00"}
    assert page.aliyun_type == "Invoice"
    assert page.warnings == []


def test_normalize_invoice_falls_back_to_content_with_warning():
    raw = {"Data": {"Content": "整页文字", "SubImages": [{}]}}
    page = ocr.normalize(raw, "invoice")
    assert page.text == "整页文字"
    assert page.fields == {}
    assert any("退回整页文本" in w for w in page.warnings)


def test_normalize_kv_details_when_data_is_json_string():
    """网关把 KvInfo.Data 序列化成 JSON 字符串时也要解析出来。"""
    raw = {"Data": {"SubImages": [{"KvInfo": {"Data": '{"发票号码": "99"}'}}]}}
    assert ocr.normalize(raw, "payment_record").text == "发票号码: 99"


def test_normalize_kv_details_fallback():
    raw = {"Data": {"SubImages": [{"KvInfo": {"KvDetails": {
        "发票号码": {"KeyName": '"发票号码"', "Value": '"888"'},
    }}}]}}
    page = ocr.normalize(raw, "invoice")
    assert page.text == "发票号码: 888"
    assert page.fields == {"发票号码": "888"}


def test_normalize_multi_subimage_labels_each_one():
    raw = {"Data": {"SubImageCount": 2, "IsMixedMode": True, "SubImages": [
        _sub(kv={"发票号码": "A1"}), _sub(kv={"发票号码": "B2"}),
    ]}}
    page = ocr.normalize(raw, "mixed_invoice")
    assert "【第 1 张】Invoice" in page.text and "发票号码: A1" in page.text
    assert "【第 2 张】Invoice" in page.text and "发票号码: B2" in page.text
    assert page.fields == {"1.发票号码": "A1", "2.发票号码": "B2"}
    assert page.subimage_count == 2 and page.is_mixed_mode is True


def test_normalize_mixed_mode_single_subimage_warns():
    raw = {"Data": {"SubImageCount": 1, "SubImages": [_sub(kv={"发票号码": "A"})]}}
    page = ocr.normalize(raw, "mixed_invoice")
    assert any("只识别出 1 张" in w for w in page.warnings)


def test_normalize_table_lines_by_row():
    raw = {"Data": {"Content": "", "SubImages": [{"TableInfo": {"TableDetails": [{
        "Header": {"Contents": ["项目", "金额"]},
        "CellDetails": [
            {"RowStart": 1, "ColumnStart": 1, "CellContent": "住宿"},
            {"RowStart": 1, "ColumnStart": 2, "CellContent": "300"},
        ],
    }]}}]}}
    page = ocr.normalize(raw, "general")
    assert page.text == "项目\n金额\n住宿 | 300"


def test_normalize_empty_marks_warning():
    page = ocr.normalize({"Data": {}}, "general")
    assert page.text == ""
    assert any("未识别出文本" in w for w in page.warnings)


# ---- 调用与重试 ----

@pytest.fixture
def configured(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ocr_engine", "aliyun")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_id", "fake-ak")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_secret", "fake-sk")
    monkeypatch.setattr(s, "ocr_timeout_seconds", 1.0)
    monkeypatch.setattr(s, "ocr_max_retries", 2)
    monkeypatch.setattr(ocr, "_sleep", lambda _s: None)  # 不退避,测试秒完
    return s


def test_recognize_retries_transient_then_succeeds(configured, monkeypatch):
    calls = []

    def fake_post(image, aliyun_type, timeout):
        calls.append(aliyun_type)
        if len(calls) < 3:
            raise ocr.OCRError("Throttling", "限流", retryable=True)
        return {"RequestId": "r", "Data": {"Content": "ok"}}

    monkeypatch.setattr(ocr, "_post_once", fake_post)
    page = ocr.recognize(b"img", "general")
    assert page.text == "ok"
    assert calls == ["Advanced"] * 3


def test_recognize_stops_at_max_retries(configured, monkeypatch):
    monkeypatch.setattr(configured, "ocr_max_retries", 1)
    monkeypatch.setattr(ocr, "_post_once",
                        lambda *a, **k: (_ for _ in ()).throw(ocr.OCRError("Throttling", "限流", retryable=True)))
    with pytest.raises(ocr.OCRError) as ei:
        ocr.recognize(b"img", "general")
    assert ei.value.code == "Throttling"


def test_recognize_does_not_retry_auth_error(configured, monkeypatch):
    calls = []

    def fake_post(image, aliyun_type, timeout):
        calls.append(1)
        raise ocr.OCRError("InvalidAccessKeyId.NotFound", "AK 不存在", retryable=False)

    monkeypatch.setattr(ocr, "_post_once", fake_post)
    with pytest.raises(ocr.OCRError):
        ocr.recognize(b"img", "invoice")
    assert len(calls) == 1  # 鉴权错误重试也是白花,直接抛


def test_recognize_rejects_unknown_type(configured):
    with pytest.raises(ValueError):
        ocr.recognize(b"img", "mixed_invoice_x")


def test_recognize_rejects_oversized_image(configured):
    with pytest.raises(ocr.OCRError) as ei:
        ocr.recognize(b"x" * (ocr.MAX_IMAGE_BYTES + 1), "general")
    assert ei.value.code == "ImageTooLarge"


def test_recognize_requires_credentials(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ocr_engine", "")
    with pytest.raises(ocr.OCRNotConfigured):
        ocr.recognize(b"img", "general")


def test_status_reports_missing_credentials(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ocr_engine", "aliyun")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_id", "")
    ok, reason = ocr.ocr_status()
    assert not ok and "OCR_ALIYUN_ACCESS_KEY_ID" in reason

    monkeypatch.setattr(s, "ocr_engine", "")
    ok, reason = ocr.ocr_status()
    assert not ok and "OCR_ENGINE" in reason
