"""RAG 抽取器:按文件扩展名把原始字节抽成纯文本(摄取 2e 的原料层)。

纯函数,吃 `(data: bytes, filename: str)` 吐 `ExtractResult`,不连 OSS / Qdrant。
设计原则:
- 坏文件 / 不支持的类型**不抛异常**,把原因写进 `error` 字段,让整批摄取不中断
  (同「零命中不崩」原则)。
- 各解析库**惰性 import**(未装也能导入本模块,真用到才报错)。
- 扫描件 PDF(无文本层)与图片(png/jpg)只标 `needs_ocr=True`,真 OCR 在 2d 接。
- `.doc` 老格式需外部工具链(LibreOffice/antiword),本步标 unsupported;`.xls` 老格式需
  xlrd(未装则标 unsupported);`.zip` v1 不解包。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field


@dataclass
class ExtractResult:
    text: str = ""                       # 抽出的正文(可能为空)
    blocks: list[str] = field(default_factory=list)  # 自然块(页/段/表行),供 2e 切块;无则 [text]
    needs_ocr: bool = False              # 图片 / 扫描 PDF → True,交 2d OCR
    ext: str = ""                        # 小写扩展名(无点)
    extractor: str = ""                  # 实际用的抽取器:pypdf / python-docx / openpyxl / ...
    error: str | None = None             # unsupported / skipped / 解析失败(不抛,标记)


# ---- 小工具 ----

def _ext(filename: str) -> str:
    """取小写扩展名(无点);无扩展名返回空串。"""
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _decode(data: bytes) -> str:
    """文本字节解码:utf-8 → gbk → gb18030 → latin-1,最后兜底 replace。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell(v) -> str:
    """表格单元格值归一成字符串。"""
    if v is None:
        return ""
    return str(v).strip()


# ---- 各类型抽取器(签名统一:(data, ext) -> ExtractResult)----

def _from_txt(data: bytes, ext: str) -> ExtractResult:
    text = _decode(data).strip()
    return ExtractResult(text=text, blocks=[text] if text else [], ext=ext, extractor="txt")


def _from_pdf(data: bytes, ext: str) -> ExtractResult:
    """电子版 PDF 抽文本层;无文本层(扫描版)→ needs_ocr,交 2d。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                pages.append(t)
        text = "\n\n".join(pages).strip()
        if not text:
            return ExtractResult(needs_ocr=True, ext=ext, extractor="pypdf")
        return ExtractResult(text=text, blocks=pages, ext=ext, extractor="pypdf")
    except Exception as exc:
        return ExtractResult(ext=ext, extractor="pypdf", error=f"pdf 解析失败: {exc}")


def _from_docx(data: bytes, ext: str) -> ExtractResult:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        blocks: list[str] = []
        for para in doc.paragraphs:
            t = para.text.strip()
            if t:
                blocks.append(t)
        for table in doc.tables:  # 表格:每行 "单元格 | 单元格"
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                line = " | ".join(c for c in cells if c)
                if line:
                    blocks.append(line)
        text = "\n".join(blocks).strip()
        return ExtractResult(text=text, blocks=blocks, ext=ext, extractor="python-docx")
    except Exception as exc:
        return ExtractResult(ext=ext, extractor="python-docx", error=f"docx 解析失败: {exc}")


def _from_xlsx(data: bytes, ext: str) -> ExtractResult:
    """xlsx 按行转带表头语义文本(每行 `列名: 值`),一 sheet 一段。"""
    try:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        blocks: list[str] = []
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            try:
                header = [_cell(h) for h in next(rows)]
            except StopIteration:
                continue  # 空表
            blocks.append(f"[表: {ws.title}]")
            for row in rows:
                if row is None:
                    continue
                pairs: list[str] = []
                for h, v in zip(header, row):
                    cv = _cell(v)
                    if cv == "":
                        continue
                    pairs.append(f"{h}: {cv}" if h else cv)
                if pairs:
                    blocks.append("; ".join(pairs))
        wb.close()
        text = "\n".join(blocks).strip()
        return ExtractResult(text=text, blocks=blocks, ext=ext, extractor="openpyxl")
    except Exception as exc:
        return ExtractResult(ext=ext, extractor="openpyxl", error=f"xlsx 解析失败: {exc}")


def _from_xls(data: bytes, ext: str) -> ExtractResult:
    """.xls 老格式:需 xlrd;未装则标 unsupported(本步后置)。"""
    try:
        import xlrd
    except Exception:
        return ExtractResult(ext=ext, extractor="none", error="unsupported: .xls 老格式需 xlrd(未安装),本步后置")
    try:
        book = xlrd.open_workbook(file_contents=data)
        blocks: list[str] = []
        for sheet in book.sheets():
            if sheet.nrows == 0:
                continue
            header = [_cell(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
            blocks.append(f"[表: {sheet.name}]")
            for r in range(1, sheet.nrows):
                pairs: list[str] = []
                for c in range(sheet.ncols):
                    cv = _cell(sheet.cell_value(r, c))
                    if cv == "":
                        continue
                    pairs.append(f"{header[c]}: {cv}" if header[c] else cv)
                if pairs:
                    blocks.append("; ".join(pairs))
        text = "\n".join(blocks).strip()
        return ExtractResult(text=text, blocks=blocks, ext=ext, extractor="xlrd")
    except Exception as exc:
        return ExtractResult(ext=ext, extractor="xlrd", error=f"xls 解析失败: {exc}")


def _from_pptx(data: bytes, ext: str) -> ExtractResult:
    try:
        from pptx import Presentation

        prs = Presentation(io.BytesIO(data))
        blocks: list[str] = []
        for i, slide in enumerate(prs.slides, 1):
            texts: list[str] = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        line = "".join(run.text for run in para.runs).strip()
                        if line:
                            texts.append(line)
                if shape.has_table:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        rl = " | ".join(c for c in cells if c)
                        if rl:
                            texts.append(rl)
            if texts:
                blocks.append(f"[幻灯片 {i}]")
                blocks.extend(texts)
        text = "\n".join(blocks).strip()
        return ExtractResult(text=text, blocks=blocks, ext=ext, extractor="python-pptx")
    except Exception as exc:
        return ExtractResult(ext=ext, extractor="python-pptx", error=f"pptx 解析失败: {exc}")


def _from_image(data: bytes, ext: str) -> ExtractResult:
    """图片:全部交 2d OCR;本步只占位标记。"""
    return ExtractResult(needs_ocr=True, ext=ext, extractor="ocr-pending")


def _unsupported_doc(data: bytes, ext: str) -> ExtractResult:
    return ExtractResult(ext=ext, extractor="none", error="unsupported: .doc 老格式需外部转换(LibreOffice/antiword),本步未接")


def _skip_zip(data: bytes, ext: str) -> ExtractResult:
    return ExtractResult(ext=ext, extractor="none", error="skipped: .zip 压缩包 v1 不解包")


_DISPATCH = {
    "pdf": _from_pdf,
    "docx": _from_docx,
    "xlsx": _from_xlsx,
    "xls": _from_xls,
    "pptx": _from_pptx,
    "txt": _from_txt,
    "png": _from_image,
    "jpg": _from_image,
    "jpeg": _from_image,
    "doc": _unsupported_doc,
    "zip": _skip_zip,
}

# 支持(能抽出文本或明确交 OCR)的扩展名,供上层预判 / 提示。
SUPPORTED_EXTS = frozenset(_DISPATCH)


def extract(data: bytes, filename: str) -> ExtractResult:
    """按扩展名分派抽取;未知扩展名标 unsupported(不抛)。"""
    ext = _ext(filename)
    fn = _DISPATCH.get(ext)
    if fn is None:
        return ExtractResult(ext=ext, extractor="none", error=f"unsupported: 未知扩展名 .{ext or '(无)'}")
    return fn(data, ext)
