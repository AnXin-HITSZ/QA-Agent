"""OCR 接口(预留):把图片 / 扫描件的字节抽成文本。

现状:只预留接口与「按 .env 切换引擎」的分发骨架,**尚未接入任何具体引擎**
(大概率选阿里云 OCR,和 OSS / DashScope 同体系)。未配置或引擎未接入时,
`ocr_image` 抛 `OCRNotConfigured`,由摄取层(2e)捕获后对 needs_ocr 的文件
**优雅降级**(跳过并告警),不阻断整批摄取——同 Redis / OSS 缺失时的降级风格。

要接入某引擎(2d),只需三处本地改动,调用方(extract / 2e 摄取)零改:
  1. 把对应的 `_ocr_*` 函数补成真实现;
  2. 把该引擎名加进 `_IMPLEMENTED`;
  3. .env 里设 `OCR_ENGINE=<引擎名>`。

扫描件 PDF 的处理留到 2d:届时先用 pypdf / PyMuPDF 把页面渲染成图,再逐页
调 `ocr_image`;本模块此刻只提供「单张图片 → 文本」这一原子能力。
"""

from __future__ import annotations

from app.config import get_settings


class OCRNotConfigured(RuntimeError):
    """未配置 / 未接入可用 OCR 引擎:上层应跳过该 needs_ocr 文件并告警,而非崩。"""


def _ocr_aliyun(data: bytes) -> str:
    """阿里云 OCR(待接入 2d):文档识别 / 通用文字识别 → 纯文本。"""
    raise NotImplementedError("阿里云 OCR 尚未接入(2d)")


def _ocr_rapidocr(data: bytes) -> str:
    """本地 RapidOCR(备选,待接入 2d)。"""
    raise NotImplementedError("RapidOCR 尚未接入(2d)")


# 引擎名 → 实现函数。接哪个就把它加进 _IMPLEMENTED 并补上函数体。
_ENGINES = {
    "aliyun": _ocr_aliyun,
    "rapidocr": _ocr_rapidocr,
}

# 已「真正接入」的引擎(占位实现不算)。目前全未接入 → OCR 整体关闭。
_IMPLEMENTED: frozenset[str] = frozenset()


def _selected() -> str:
    return (get_settings().ocr_engine or "").strip().lower()


def ocr_available() -> bool:
    """是否配了「已接入」的 OCR 引擎。2e 摄取前先问它,避免逐个文件白白抛异常。"""
    engine = _selected()
    return engine in _ENGINES and engine in _IMPLEMENTED


def ocr_image(data: bytes) -> str:
    """单张图片字节 → 文本。未配置 / 未接入 / 未知引擎 → 抛 OCRNotConfigured。"""
    engine = _selected()
    if not engine:
        raise OCRNotConfigured("未配置 OCR_ENGINE;needs_ocr 文件将在摄取时跳过")
    impl = _ENGINES.get(engine)
    if impl is None:
        raise OCRNotConfigured(
            f"未知 OCR_ENGINE={engine!r}(可选:{', '.join(sorted(_ENGINES))})"
        )
    if engine not in _IMPLEMENTED:
        raise OCRNotConfigured(f"OCR 引擎 {engine!r} 尚未接入(2d);needs_ocr 文件将被跳过")
    return impl(data)
