"""逐页提取:四种方式、非法组合、PDF 全页、混贴选项、两层缓存与失败定位。

不调用真实 OCR:把 ocr.recognize_raw(网络边界)换成给定响应 —— 两层缓存、归一化、
逐页编排仍真实执行,所以缓存命中 / 失效的断言是真断言。缓存是进程内 MemoryCache。
PDF 渲染用 pdfium 真渲染(小尺寸页面,快)。
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from app.rag import document_extract as de
from app.rag import ocr, ocr_cache
from app.rag.document_extract import extract_document
from tests import ocrkit
from tests.pdfkit import make_pdf


@pytest.fixture
def env(cache_env, monkeypatch, tmp_path):
    """配置好 OCR(假凭证)+ 内存缓存;默认返回单页文本的假响应。"""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "index_state_dir", str(tmp_path / "ocr"))
    monkeypatch.setattr(s, "ocr_engine", "aliyun")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_id", "fake-ak")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_secret", "fake-sk")
    monkeypatch.setattr(s, "ocr_render_dpi", 72)
    calls: list[tuple[int, str]] = []

    def fake(text: str = "识别文本", fields: dict | None = None, raise_exc: Exception | None = None):
        # 每次调用换掉 recognize_raw 的实现;monkeypatch 会在测试结束后依次还原。
        return ocrkit.install(monkeypatch, text=text, fields=fields, calls=calls, boom=raise_exc)

    fake()
    return SimpleNamespace(calls=calls, fake=fake, settings=s, cache=cache_env, tmp=tmp_path)


# ---- 参数组合 ----

def test_effective_mode_expands_mixed_invoice():
    assert de.effective_mode("invoice", True) == "mixed_invoice"
    assert de.effective_mode("invoice", False) == "invoice"
    assert de.effective_mode("general") == "general"


@pytest.mark.parametrize("mode,mixed", [("native_only", True), ("general", True), ("payment_record", True)])
def test_illegal_combinations_rejected(mode, mixed):
    with pytest.raises(ValueError):
        de.effective_mode(mode, mixed)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        extract_document(b"x", "a.pdf", "auto")


# ---- native 方式 ----

def test_native_only_reads_text_layer_per_page():
    # 测试 PDF 用内置 Helvetica(仅 ASCII):只关心"页码与文本层逐页保留"
    data = make_pdf(pages=[{"text": "page one text"}, {"text": "page two text"}])
    res = extract_document(data, "a.pdf", "native_only")
    assert [u.page for u in res.units] == [1, 2]
    assert "page one text" in res.text and "page two text" in res.text
    assert res.incomplete is False and res.needs_ocr is False
    assert res.methods == ["native"]


def test_native_only_marks_scan_page_incomplete_and_needs_ocr():
    """整个文件都是扫描页 → needs_ocr,并逐页记录原因(不静默丢弃)。"""
    data = make_pdf(pages=[{"scan": True}, {"scan": True}])
    res = extract_document(data, "scan.pdf", "native_only")
    assert res.needs_ocr is True
    assert res.incomplete is True
    assert [p.status for p in res.pages] == ["incomplete", "incomplete"]
    assert all(p.error == "no_text_layer" for p in res.pages)
    assert "第1页" in de.incomplete_reason(res)


def test_native_only_keeps_blank_page_out_of_failures():
    data = make_pdf(pages=[{"text": "some body text"}, {}])
    res = extract_document(data, "mix.pdf", "native_only")
    assert [p.status for p in res.pages] == ["success", "blank"]
    assert res.incomplete is False        # 空白页不算失败
    assert [u.page for u in res.units] == [1]


def test_native_only_image_needs_ocr():
    res = extract_document(b"fake-png-bytes", "photo.jpg", "native_only")
    assert res.needs_ocr is True and res.units == []
    assert res.pages[0].error == "no_text_layer"


def test_office_under_ocr_mode_uses_native_with_warning():
    """Office 格式不支持 OCR:按原生抽取并明确告知,而不是沉默或报错。"""
    docx = _make_docx("会议纪要内容")
    res = extract_document(docx, "notes.docx", "general")
    assert "会议纪要内容" in res.text
    assert res.method == "native"
    assert any("不支持 OCR" in w for w in res.warnings)


# ---- OCR 方式:PDF 全页 ----

def test_ocr_mode_covers_every_page_including_text_layer(env):
    """OCR 模式对每一页都调用识别(含本来有文本层的页),不自动改走原生。"""
    data = make_pdf(pages=[{"text": "page with a text layer"}, {"scan": True}, {}])
    env.fake(text="识别结果")
    res = extract_document(data, "mix.pdf", "general")
    assert [u.page for u in res.units] == [1, 2]        # 第 3 页空白,跳过但保留判定
    assert [p.status for p in res.pages] == ["success", "success", "blank"]
    assert len(env.calls) == 2                          # 只对非空白页调 OCR
    assert all(c[1] == "general" for c in env.calls)
    assert res.incomplete is False


def test_ocr_mode_records_failed_page_and_marks_incomplete(env):
    def _boom(image, ocr_type, **kw):
        raise ocr.OCRError("Throttling", "限流")

    env.fake(raise_exc=ocr.OCRError("Throttling", "限流"))
    data = make_pdf(pages=[{"scan": True}])
    res = extract_document(data, "scan.pdf", "general")
    assert res.pages[0].status == "failed"
    assert "Throttling" in res.pages[0].error
    assert res.incomplete is True
    assert res.units == []


def test_ocr_empty_text_on_non_blank_page_is_incomplete(env):
    env.fake(text="   ")
    data = make_pdf(pages=[{"scan": True}])
    res = extract_document(data, "scan.pdf", "invoice")
    assert res.pages[0].status == "incomplete"
    assert res.pages[0].error == "ocr_empty"
    assert res.incomplete is True


def test_invoice_fields_marked_from_fields(env):
    env.fake(text="发票号码: 1", fields={"发票号码": "1"})
    data = make_pdf(pages=[{"scan": True}])
    res = extract_document(data, "f.pdf", "invoice")
    assert res.units[0].from_fields is True
    assert res.pages[0].fields == {"发票号码": "1"}


def test_render_pdf_pages_uses_pdfium(env):
    """真渲染一遍,确认 pdfium 调用方式(scale / to_pil / close)可用。"""
    data = make_pdf(pages=[{"text": "x"}, {"text": "y"}])
    pages = list(de.render_pdf_pages(data, 72, 25_000_000))
    assert [n for n, _ in pages] == [1, 2]
    assert all(png.startswith(b"\x89PNG") for _, png in pages)


def test_blank_detection(env):
    from PIL import Image

    def png(color: int) -> bytes:
        buf = io.BytesIO()
        Image.new("L", (50, 50), color=color).save(buf, "PNG")
        return buf.getvalue()

    assert de.looks_blank(png(255)) is True
    assert de.looks_blank(png(0)) is False


def test_oversized_page_is_downscaled(env):
    data = make_pdf(pages=[{"text": "x"}], size=(600, 600))
    (page_no, png), = de.render_pdf_pages(data, 200, max_pixels=1000)
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        assert im.width * im.height <= 1000 * 1.1


# ---- 缓存(两层:识别结果 / 文本转换) ----

def test_cache_hit_avoids_second_call(env):
    data = make_pdf(pages=[{"scan": True}])
    first = extract_document(data, "s.pdf", "general")
    second = extract_document(data, "s.pdf", "general")
    assert len(env.calls) == 1
    assert first.pages[0].cached is False
    assert second.pages[0].cached is True
    assert second.text == first.text


def test_cache_key_changes_with_ocr_type(env):
    data = make_pdf(pages=[{"scan": True}])
    extract_document(data, "s.pdf", "general")
    extract_document(data, "s.pdf", "invoice")
    assert len(env.calls) == 2                    # 换 Type 不误用旧缓存
    assert [c[1] for c in env.calls] == ["general", "invoice"]


def test_same_image_in_another_file_hits_cache(env):
    """§4.1:页码与整份文件哈希不进识别键 —— 同一张图换个文件照样命中,不重复付费。"""
    data = make_pdf(pages=[{"scan": True}])
    extract_document(data, "first.pdf", "general")
    res = extract_document(data, "second.pdf", "general")   # 同一份字节、不同文件名
    assert len(env.calls) == 1
    assert res.pages[0].cached is True


def test_same_image_on_another_page_hits_cache(env):
    """同一张图出现在两页 → 第二页命中第一次的识别缓存(页码不参与键)。"""
    one = make_pdf(pages=[{"scan": True}])
    two = make_pdf(pages=[{"scan": True}, {"scan": True}])
    extract_document(one, "single.pdf", "general")
    res = extract_document(two, "double.pdf", "general")
    assert [p.status for p in res.pages] == ["success", "success"]
    assert len(env.calls) == 1                       # 两页渲染出的字节相同 → 只调一次
    assert res.pages[1].cached is True


def test_refresh_ocr_bypasses_cache(env):
    data = make_pdf(pages=[{"scan": True}])
    extract_document(data, "s.pdf", "general")
    res = extract_document(data, "s.pdf", "general", refresh=True)
    assert len(env.calls) == 2
    assert res.pages[0].cached is False


def test_text_rules_change_reconverts_without_ocr(env, monkeypatch):
    """§4.2:只改转换规则版本 → 从识别缓存重新转换,不再调供应商,也不重复计费。"""
    data = make_pdf(pages=[{"scan": True}])
    first = extract_document(data, "s.pdf", "general")
    assert len(env.calls) == 1
    raw_key = first.pages[0].ocr_cache_key
    assert env.cache.get(raw_key) is not None            # 识别结果留在缓存里

    monkeypatch.setattr(ocr_cache, "TEXT_RULES_VERSION", "2")
    env.fake(text="不应被调用")                            # 真调了就会改写正文
    second = extract_document(data, "s.pdf", "general")
    assert len(env.calls) == 1                           # 没再调 OCR
    assert second.text == first.text                     # 正文来自识别缓存重新转换
    assert second.pages[0].cached is True                # 第 1 层命中
    assert second.pages[0].text_cache_key != first.pages[0].text_cache_key
    assert env.cache.get(first.pages[0].text_cache_key) is not None   # 旧规则结果没被删


def test_failed_page_is_not_cached(env):
    env.fake(raise_exc=ocr.OCRError("Throttling", "限流"))
    data = make_pdf(pages=[{"scan": True}])
    extract_document(data, "s.pdf", "general")
    env.fake(text="第二次成功")            # 换掉实现,模拟供应商恢复
    res = extract_document(data, "s.pdf", "general")
    assert res.pages[0].status == "success"
    assert res.pages[0].cached is False    # 失败没进缓存,所以没有命中


def test_empty_result_is_not_cached(env):
    """§4.1:归一化后正文为空不算成功,不写缓存 —— 下次仍会重新识别。"""
    env.fake(text="   ")
    data = make_pdf(pages=[{"scan": True}])
    extract_document(data, "s.pdf", "general")
    res = extract_document(data, "s.pdf", "general")
    assert res.pages[0].status == "incomplete"
    assert len(env.calls) == 2             # 第一次的空结果没进缓存


def test_cache_hit_keeps_field_marker_without_storing_field_set(env):
    """§10:缓存只留 recognized_fields 标记与正文,不另存票据字段集;命中后标记不丢。"""
    env.fake(text="发票号码: 1", fields={"发票号码": "1"})
    data = make_pdf(pages=[{"scan": True}])
    first = extract_document(data, "f.pdf", "invoice")
    assert first.units[0].from_fields is True
    assert first.pages[0].fields == {"发票号码": "1"}          # 本次调用内存里仍有字段明细

    env.fake(text="不应被调用", fields=None)                    # 再调用就说明没命中缓存
    second = extract_document(data, "f.pdf", "invoice")
    assert len(env.calls) == 1 and second.pages[0].cached is True
    assert second.units[0].from_fields is True                  # 标记保留
    assert second.pages[0].fields == {}                         # 字段明细不落缓存

    key = second.pages[0].text_cache_key
    payload = json.loads(env.cache.get(key))
    assert payload["recognized_fields"] is True
    assert "fields" not in payload


def test_cache_stores_raw_for_diagnosis(env):
    env.fake(text="ok")
    data = make_pdf(pages=[{"scan": True}])
    res = extract_document(data, "s.pdf", "general")
    payload = json.loads(env.cache.get(res.pages[0].ocr_cache_key))
    assert payload["raw"]["Data"]["Content"] == "ok"
    assert payload["request_id"] == "req-test"


def test_empty_file_returns_reason_without_raising():
    res = extract_document(b"", "x.pdf", "general")
    assert res.error and "empty" in res.error


def test_corrupt_pdf_reports_error(env):
    res = extract_document(b"not a pdf at all", "bad.pdf", "general")
    assert res.error and "pdf" in res.error


def _make_docx(text: str) -> bytes:
    from docx import Document

    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
