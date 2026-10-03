"""生成测试用 PDF 的小工具(不引入 reportlab 等额外依赖)。

make_pdf(pages=[...]) 每页三种形态:
  {"text": "..."}  带文本层(Helvetica 标准字体)
  {"scan": True}   只有一张位图(模拟扫描页,没有文本层)
  {}               空白页(无内容、无图)
"""

from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

_TEXT_TEMPLATE = "BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
_IMAGE_DRAW = "q {w} 0 0 {h} 0 0 cm /Im1 Do Q"


def _font() -> DictionaryObject:
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })


def _gray_raw() -> bytes:
    """8×8 灰度原始像素(DeviceGray / 8bpc 不需要滤镜,是合法图像流)。"""
    from PIL import Image

    return Image.new("L", (8, 8), color=120).tobytes()


def _image_xobject(writer: PdfWriter, raw: bytes) -> object:
    """把原始灰度像素塞进一个最小 Image XObject(测试只关心"这一页有位图")。"""
    w = h = 8
    obj = DecodedStreamObject()
    obj.set_data(raw)
    obj.update(DictionaryObject({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(w),
        NameObject("/Height"): NumberObject(h),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    }))
    return writer._add_object(obj)


def make_pdf(pages: list[dict] | None = None, size: tuple[int, int] = (300, 300)) -> bytes:
    writer = PdfWriter()
    width, height = size
    for spec in pages if pages is not None else [{"text": "hello"}]:
        page = writer.add_blank_page(width=width, height=height)
        resources = DictionaryObject()
        if spec.get("scan"):
            ref = _image_xobject(writer, _gray_raw())
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): ref})
            content = DecodedStreamObject()
            content.set_data(_IMAGE_DRAW.format(w=width, h=height).encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(content)
        elif spec.get("text"):
            resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): _font()})
            content = DecodedStreamObject()
            content.set_data(_TEXT_TEMPLATE.format(text=spec["text"]).encode("latin-1", "replace"))
            page[NameObject("/Contents")] = writer._add_object(content)
        if len(resources):
            page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
