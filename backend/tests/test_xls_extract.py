"""老版 Excel(.xls)逐表抽取:表头语义、空单元格 / 空行 / 空表、多表与列数不齐。

造数据分两层:
- 多数用例把 xlrd 换成假模块(不碰真二进制格式,快且确定),验证的是我们这一侧的
  成形逻辑(「列名: 值」、[表: 名] 标记、跳过规则);
- 末尾一条真文件往返:openpyxl 写 xlsx → LibreOffice 转 .xls → 真 xlrd 读回,
  验证与真 BIFF 文件、真库接口对得上 —— 只在装了 soffice 的机器上跑。
"""

from __future__ import annotations

import sys
import types

import pytest

from app.rag.extract import extract
from tests import sofficekit


class FakeSheet:
    """xlrd Sheet 的替身:只实现 _from_xls 用到的四个成员,越界单元格返回空串。"""

    def __init__(self, name: str, rows: list[list]) -> None:
        self.name = name
        self._rows = rows

    @property
    def nrows(self) -> int:
        return len(self._rows)

    @property
    def ncols(self) -> int:
        return max((len(r) for r in self._rows), default=0)

    def cell_value(self, r: int, c: int):
        row = self._rows[r]
        return row[c] if c < len(row) else ""


@pytest.fixture
def fake_xlrd(monkeypatch):
    """把 sys.modules 里的 xlrd 换成假模块;传一组 FakeSheet,open_workbook 原样返回。"""

    def install(sheets: list[FakeSheet]) -> None:
        mod = types.ModuleType("xlrd")
        mod.open_workbook = lambda **kw: types.SimpleNamespace(sheets=lambda: sheets)
        monkeypatch.setitem(sys.modules, "xlrd", mod)

    return install


# ---- 成形逻辑(假 xlrd) ----

def test_header_row_becomes_named_pairs(fake_xlrd):
    # 真 xlrd 的数字一律是 float,替身照着回落,免得测试里出现真机不会有的 int
    fake_xlrd([FakeSheet("差旅", [["项目", "金额"], ["高铁票", 1200.0], ["住宿", 800.0]])])
    res = extract(b"dummy", "账目.xls")
    assert res.error is None and res.extractor == "xlrd"
    assert res.blocks == [
        "[表: 差旅]",
        "项目: 高铁票; 金额: 1200",
        "项目: 住宿; 金额: 800",
    ]
    assert res.text == "\n".join(res.blocks)


def test_whole_floats_become_int_decimals_kept(fake_xlrd):
    """整数值的浮点归一成 int(「1200」不是「1200.0」);真小数原样保留。"""
    fake_xlrd([FakeSheet("S", [["n"], [1200.0], [1200.5], [0.0]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: S]", "n: 1200", "n: 1200.5", "n: 0"]


def test_empty_cells_and_blank_rows_are_skipped(fake_xlrd):
    fake_xlrd([FakeSheet("S", [["a", "b", "c"], ["1", "", None], ["", "", ""]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: S]", "a: 1"]      # 空单元格不输出,整行空不产生行


def test_short_row_does_not_misalign(fake_xlrd):
    """行比表头短(真 xlrd 里尾部单元格就是空)—— 只输出有值的列,不串列。"""
    fake_xlrd([FakeSheet("S", [["a", "b"], ["1"]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: S]", "a: 1"]


def test_headerless_column_falls_back_to_value(fake_xlrd):
    fake_xlrd([FakeSheet("S", [["", "金额"], ["备注A", 5.0]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: S]", "备注A; 金额: 5"]


def test_empty_sheet_leaves_no_marker(fake_xlrd):
    fake_xlrd([FakeSheet("空表", []), FakeSheet("有货", [["x"], ["1"]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: 有货]", "x: 1"]


def test_multiple_sheets_keep_their_names(fake_xlrd):
    fake_xlrd([FakeSheet("一月", [["x"], ["1"]]), FakeSheet("二月", [["x"], ["2"]])])
    res = extract(b"dummy", "x.xls")
    assert res.blocks == ["[表: 一月]", "x: 1", "[表: 二月]", "x: 2"]


# ---- 依赖与坏文件 ----

def test_missing_xlrd_still_marks_unsupported(monkeypatch):
    """依赖没装(如生产环境没跑 pip install -r requirements.txt)只标记、不抛。"""
    monkeypatch.setitem(sys.modules, "xlrd", None)      # import 时会抛 ImportError
    res = extract(b"dummy", "old.xls")
    assert res.text == "" and res.extractor == "none"
    assert res.error and "xlrd" in res.error and "未安装" in res.error


def test_broken_xls_reports_parse_error_not_missing_dep():
    """xlrd 已就位时,坏文件走「解析失败」,不再是被跳过的那句「需 xlrd」。"""
    res = extract(b"not an xls at all", "old.xls")
    assert res.error and "解析失败" in res.error and "未安装" not in res.error


# ---- 真文件往返(本机没装 soffice 就跳过) ----

@pytest.mark.skipif(not sofficekit.REAL_SOFFICE, reason="本机没有 LibreOffice(soffice)")
def test_real_xls_roundtrip(tmp_path):
    """xlsx → .xls(真 soffice) → 真 xlrd 抽取。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "报销"
    ws.append(["项目", "金额"])
    ws.append(["高铁票", 1200])
    xlsx = tmp_path / "seed.xlsx"
    wb.save(str(xlsx))

    xls = sofficekit.convert(xlsx, "xls", tmp_path)
    res = extract(xls.read_bytes(), "seed.xls")
    assert res.error is None, res.error
    assert res.extractor == "xlrd"
    assert "[表: 报销]" in res.blocks
    # BIFF 数字是浮点、xlrd 交回 1200.0,文本里应是归一后的 "1200"(见 _xls_num)
    assert "项目: 高铁票; 金额: 1200" in res.blocks, res.blocks
