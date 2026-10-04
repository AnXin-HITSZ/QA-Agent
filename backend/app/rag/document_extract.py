"""文档逐页提取:按用户选定的提取方式,把一份原件抽成「带页码的文本单元」。

提取方式(前端手选,不做自动分类、不按目录前缀路由、不自动切换识别类型):

  不使用 OCR        native_only       只取 PDF / Office 的原生文本层;扫描页记录为「无文本层」
  通用文字基础版    general           每页渲染后调 OCR(General)
  通用文字高精版    general_advanced  每页渲染后调 OCR(Advanced)
  发票识别          invoice           每页渲染后调 OCR(Invoice)
  混贴票据页        mixed_invoice     同上,但用 MixedInvoice(一页多张票据)
  付款详情识别      payment_record    每页渲染后调 OCR(PaymentRecord)

「混贴票据页」是发票识别的子选项(前端勾选框),在这里是独立的 mode 取值,便于接口校验。

行为约定:
- OCR 方式 = 强制对 PDF 每一页调用识别(含本来有文本层的页),不静默改走原生 —— 这是
  用户显式选择的结果,不是系统替他判断。想省钱就选「不使用 OCR」。
- 空白页跳过但不算失败(保留页码与 blank 判定记录);有内容却抽不出文本的页算 incomplete,
  整份文件默认不发布(见 ingest),避免把半截内容当成完整结果写进索引。
- Office / 纯文本格式始终走原生抽取;在 OCR 方式下会记一条告警,提示该格式不支持 OCR。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Iterator

from app.config import get_settings
from app.metering.context import bind as bind_context
from app.rag import ocr_cache
from app.rag.cache_store import CacheError
from app.rag.extract import extract

logger = logging.getLogger(__name__)

NATIVE_ONLY = "native_only"
INVOICE = "invoice"
MIXED_INVOICE = "mixed_invoice"
OCR_MODES = ("general", "general_advanced", INVOICE, MIXED_INVOICE, "payment_record")
ALL_MODES = (NATIVE_ONLY,) + OCR_MODES
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg"})

# 深色像素占比低于此值 → 判为空白页(扫描件里真正的空白页墨点极少)。
# 阈值取 0.05%:200 DPI 的 A4 约等于 1900 个深色像素 —— 空白页的噪点够不着,
# 但是哪怕只有一行短文字也会超过它,不会把"内容很少"的页误判成空白。
_BLANK_DARK_RATIO = 0.0005
_BLANK_PIXEL_LEVEL = 200  # 灰度低于它算「深色」

EXTRACTION_VERSION = "1"  # 逐页提取策略版本,写进切块 payload 便于追溯


def effective_mode(extraction_mode: str, mixed_invoice: bool = False) -> str:
    """接口参数 → 内部提取方式。「混贴票据页」是发票模式下的勾选项,不是独立模式。

    非法组合(native_only + 勾选、通用识别 + 勾选)直接抛 ValueError,由接口层转 422。
    """
    if mixed_invoice:
        if extraction_mode != INVOICE:
            raise ValueError("mixed_invoice=true 只允许配合 extraction_mode=invoice")
        return MIXED_INVOICE
    if extraction_mode not in ALL_MODES:
        raise ValueError(f"未知提取方式 {extraction_mode!r}(可选:{', '.join(ALL_MODES)})")
    return extraction_mode


@dataclass
class PageResult:
    """一页的提取结果(页码从 1 开始,与原件一致)。"""

    page_number: int
    status: str            # success / blank / incomplete / failed
    method: str            # native / ocr
    text: str = ""
    ocr_type: str | None = None
    cached: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    fields: dict = field(default_factory=dict)
    # 该页用到的缓存键(供文件清单记录当前发布版本用了哪些缓存,§7);原生 / 空白页为 None。
    ocr_cache_key: str | None = None
    text_cache_key: str | None = None


@dataclass
class TextUnit:
    """切块前的自然单元:一段文本 + 它来自哪一页(Office 类无页码,page=None)。"""

    page: int | None
    text: str
    from_fields: bool = False   # 文本来自专用接口的结构化字段(票据 / 付款详情)


@dataclass
class DocumentResult:
    units: list[TextUnit] = field(default_factory=list)
    pages: list[PageResult] = field(default_factory=list)
    method: str = ""                 # native / ocr / mixed(逐页可能不同)
    ext: str = ""
    extractor: str = ""
    needs_ocr: bool = False          # native 方式下整份都没有文本层 → 需换 OCR 方式
    incomplete: bool = False         # 有页失败或该有文本却没抽到 → 默认不发布
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    extraction_version: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(u.text for u in self.units if u.text)

    @property
    def methods(self) -> list[str]:
        """本文件实际用过的提取方法(native / ocr),去重且稳定排序。"""
        return sorted({p.method for p in self.pages}) or (["native"] if self.units else [])


# ---- 渲染 ----

def render_pdf_pages(data: bytes, dpi: int, max_pixels: int) -> Iterator[tuple[int, bytes]]:
    """逐页渲染 PDF → (页码, PNG 字节)。惰性 yield:一页处理完再渲染下一页,不整本进内存。

    超过像素上限时按比例降采样,避免超大页面撑爆内存或超出接口限制。
    """
    import pypdfium2 as pdfium  # 惰性 import:未用到 OCR 的环境不受影响

    pdf = pdfium.PdfDocument(data)
    try:
        scale = max(0.1, dpi / 72.0)
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                img = page.render(scale=scale).to_pil()
                if max_pixels > 0 and img.width * img.height > max_pixels:
                    ratio = (max_pixels / (img.width * img.height)) ** 0.5
                    img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                yield i + 1, buf.getvalue()
            finally:
                page.close()
    finally:
        pdf.close()


def looks_blank(png: bytes) -> bool:
    """渲染图是否近似空白(深色像素占比极低)。用于区分「空白页」与「识别失败」。"""
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(io.BytesIO(png)) as im:
            hist = im.convert("L").histogram()
    except Exception:
        return False
    total = sum(hist)
    if not total:
        return True
    dark = sum(hist[:_BLANK_PIXEL_LEVEL])
    return dark / total < _BLANK_DARK_RATIO


# ---- 原生 PDF 逐页 ----

def _pdf_page_has_images(page) -> bool:
    """页面资源里是否挂了位图(扫描件一页就是一张图)。取不到信息时按「有图」处理,宁可保守。"""
    try:
        res = page.get("/Resources")
        if res is None:
            return False
        res = res.get_object()
        xobj = res.get("/XObject")
        if xobj is None:
            return False
        for key in xobj.get_object():
            try:
                obj = xobj.get_object()[key].get_object()
            except Exception:
                continue
            if obj.get("/Subtype") == "/Image":
                return True
        return False
    except Exception:
        return True


def _extract_pdf(data: bytes, mode: str, refresh: bool = False) -> DocumentResult:
    """PDF:OCR 方式逐页渲染后识别;原生方式逐页取文本层。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    result = DocumentResult(ext="pdf", extractor="pypdf",
                            method="native" if mode == NATIVE_ONLY else "ocr",
                            extraction_version=f"pdf:{EXTRACTION_VERSION}")

    if mode == NATIVE_ONLY:
        empty_pages: list[int] = []
        for i, page in enumerate(reader.pages, 1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                result.pages.append(PageResult(i, "failed", "native", error=f"文本层解析失败:{exc}"))
                continue
            if text:
                result.pages.append(PageResult(i, "success", "native", text=text))
                result.units.append(TextUnit(i, text))
            elif _pdf_page_has_images(page):
                result.pages.append(PageResult(
                    i, "incomplete", "native", error="no_text_layer",
                    warnings=["该页是扫描图,缺少文本层"],
                ))
                empty_pages.append(i)
            else:
                result.pages.append(PageResult(i, "blank", "native"))
        result.incomplete = any(p.status in ("incomplete", "failed") for p in result.pages)
        if not result.units and empty_pages:
            result.needs_ocr = True
            result.extractor = "pypdf(需 OCR)"
        return result

    # OCR 方式:逐页渲染 → 识别(结果进两层缓存)
    dpi = get_settings().ocr_render_dpi
    max_pixels = get_settings().ocr_max_page_pixels
    seen = 0
    for page_no, png in render_pdf_pages(data, dpi, max_pixels):
        seen += 1
        _ocr_page_into(result, page_no, png, mode, refresh)
    if seen < page_count:  # 渲染中途少页(异常被吞)也算不完整,不静默放过
        result.incomplete = True
        result.warnings.append(f"只渲染出 {seen} 页,原件共 {page_count} 页")
    result.incomplete = result.incomplete or any(
        p.status in ("incomplete", "failed") for p in result.pages
    )
    result.extractor = f"pdfium + {mode}"
    return result


def _ocr_page_into(result: DocumentResult, page_no: int, png: bytes, mode: str,
                   refresh: bool = False) -> None:
    """识别一页并写入结果(缓存命中也走同一路径,只是不调供应商)。"""
    if looks_blank(png):
        result.pages.append(PageResult(page_no, "blank", "ocr"))
        return
    try:
        # 调用日志归属:这一页的识别 / 缓存命中都记在本页(§3)
        with bind_context(page_no=page_no):
            got = recognize_page(png, mode, refresh)
    except CacheError:
        raise                  # 缓存不可用 / 写失败:整批停下来报错,不当作"这一页失败"(§9)
    except Exception as exc:  # OCR 失败(QCRError / 网络 / 未配置)都记在该页上,不中断整批
        result.pages.append(PageResult(
            page_no, "failed", "ocr", ocr_type=mode, error=f"{type(exc).__name__}:{exc}",
        ))
        return
    page = got.page
    text = (page.text or "").strip()
    warnings = list(page.warnings)
    if not text:
        result.pages.append(PageResult(
            page_no, "incomplete", "ocr", ocr_type=mode, cached=got.ocr_hit,
            warnings=warnings + ["非空白页但未识别出文本"], error="ocr_empty",
        ))
        return
    result.pages.append(PageResult(
        page_no, "success", "ocr", text=text, ocr_type=mode, cached=got.ocr_hit,
        warnings=warnings, fields=page.fields,
        ocr_cache_key=got.ocr_cache_key, text_cache_key=got.text_cache_key,
    ))
    # 缓存命中时 fields 明细不恢复,靠 recognized_fields 标记(缓存里保存的那个布尔值)。
    result.units.append(TextUnit(page_no, text, from_fields=bool(page.recognized_fields or page.fields)))


def recognize_page(png: bytes, ocr_type: str, refresh: bool = False) -> ocr_cache.CachedPage:
    """单页两层缓存识别。测试里替换本函数即可离线跑。

    识别缓存按「图片字节 + Type + 配置」命中:同一张图换个页码 / 换个文件照样命中;
    文本转换缓存按「有效内容哈希 + 规则版本」命中:改转换规则只重转换,不调 OCR。
    """
    return ocr_cache.recognize_cached(png, ocr_type, refresh=refresh)


# ---- 图片与其它格式 ----

def _extract_image(data: bytes, ext: str, mode: str, refresh: bool = False) -> DocumentResult:
    """图片:单页。OCR 方式直接识别原图;原生方式标记需 OCR。"""
    result = DocumentResult(ext=ext, extractor="image", method="ocr" if mode != NATIVE_ONLY else "native",
                            extraction_version=f"image:{EXTRACTION_VERSION}")
    if mode == NATIVE_ONLY:
        result.needs_ocr = True
        result.pages.append(PageResult(1, "incomplete", "native", error="no_text_layer",
                                       warnings=["图片没有文本层,需选一种 OCR 提取方式"]))
        return result
    _ocr_page_into(result, 1, data, mode, refresh)  # 原图直接提交,识别缓存按图片字节命中
    result.incomplete = any(p.status in ("incomplete", "failed") for p in result.pages)
    result.extractor = f"image + {mode}"
    return result


def _extract_other(data: bytes, filename: str, mode: str, ext: str) -> DocumentResult:
    """Office / 纯文本等:始终原生抽取;选了 OCR 方式时明确提示该格式不支持 OCR。"""
    res = extract(data, filename)
    result = DocumentResult(ext=res.ext or ext, extractor=res.extractor, method="native",
                            error=res.error, extraction_version=f"native:{EXTRACTION_VERSION}")
    if mode != NATIVE_ONLY and not res.error:
        result.warnings.append("该格式不支持 OCR,已按原生文本抽取")
    if res.text:
        result.units = [TextUnit(None, u) for u in (res.blocks or [res.text]) if u and u.strip()]
    else:
        result.needs_ocr = res.needs_ocr
    return result


def extract_document(data: bytes, filename: str, mode: str, refresh: bool = False) -> DocumentResult:
    """按提取方式把整份原件抽成带页码的文本单元。坏文件不抛异常,原因写在 result 里。

    缓存错误(CacheError)不在这里吞:它意味着整批要停下来报错(方案 §9),交调用方处理。
    """
    if mode not in ALL_MODES:
        raise ValueError(f"未知提取方式 {mode!r}(可选:{', '.join(ALL_MODES)})")
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if not data:
        return DocumentResult(ext=ext, error="empty:文件为空", extraction_version=EXTRACTION_VERSION)
    if ext == "pdf":
        try:
            return _extract_pdf(data, mode, refresh)
        except CacheError:
            raise
        except Exception as exc:
            logger.warning("PDF 提取失败 %s:%s", filename, exc)
            return DocumentResult(ext=ext, extractor="pdfium/pypdf", method="ocr" if mode != NATIVE_ONLY else "native",
                                  error=f"pdf 提取失败:{type(exc).__name__}:{exc}",
                                  extraction_version=EXTRACTION_VERSION)
    if ext in IMAGE_EXTS:
        return _extract_image(data, ext, mode, refresh)
    return _extract_other(data, filename, mode, ext)


def incomplete_reason(result: DocumentResult) -> str:
    """把「不完整」翻译成给用户看的一句话(哪几页、为什么)。"""
    bad = [p for p in result.pages if p.status in ("incomplete", "failed")]
    if not bad:
        return ""
    shown = ", ".join(f"第{p.page_number}页({p.error or p.status})" for p in bad[:5])
    more = f" 等 {len(bad)} 页" if len(bad) > 5 else ""
    return f"incomplete:有 {len(bad)} 页未能提取文本 —— {shown}{more}"
