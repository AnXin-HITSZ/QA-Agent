"""从知识库相对 key(路径 + 文件名)解析结构化富化字段(摄取时进 Qdrant payload)。

**与分类正交**:分类的权威来源是用户在前端手建的 OSS 节点前缀;这里只是"顺手"从命名
规范里抽出 project / person / 发票金额 / 费用大类 / doc_type 等,作为向量点的 payload
富化,让「关于 xxx 专利的发票在不在」「某项目材料费花了多少 / 预算多少」这类查询能按
字段过滤或核对。纯正则,无外部依赖;解析不出的字段留空(不塞进结果)。

真实语料的命名规范(来自 D:\\HITSZ\\L1716 勘察):
- 费用大类文件夹:`2科研材料及事务费116389.44-120000` = 序号? + 大类名(以"费"结尾) +
  实际支出 + '-' + 预算上限。
- 发票文件名带金额:`发票6100` / `发票8900_专利` / `发票 5560`。
- 人名挂在段尾:`一种xxx方法-郑漫莎/` 、`研究人员统计表-张三.xlsx`。
"""

from __future__ import annotations

import re

# 费用大类目录:序号?(可空) + 大类名(汉字/字母,以"费"结尾) + 实际支出 + 分隔 + 预算上限
_FEE_DIR = re.compile(
    r"^\d*\s*([\u4e00-\u9fa5A-Za-z（）()·、]+?费)\s*([\d.]+)\s*[-~至]\s*([\d.]+)$"
)
# 发票金额:文件名里"发票"后紧跟(可夹极少量非数字)的数字
_INVOICE = re.compile(r"发票[^\d]{0,4}(\d+(?:\.\d+)?)")
# 段尾人名:以 - / — 接 2~4 个汉字结尾
_PERSON = re.compile(r"[-—]([\u4e00-\u9fa5]{2,4})$")

# doc_type 关键词粗判(顺序有意义:发票先于专利,故"发票_专利"判为 invoice)
_DOC_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("invoice", ("发票",)),
    ("payment", ("付款截图", "付款", "转账")),
    ("license", ("营业执照",)),
    ("contract", ("合同", "劳务")),
    ("certificate", ("证书", "学历", "学位", "职称", "软著", "软件著作权")),
    ("statistics", ("统计表", "人才库", "资料清单")),
    ("audit", ("审计",)),
    ("paper", ("论文",)),
    ("application", ("申请书", "申请")),
    ("patent", ("专利",)),
]


def _guess_doc_type(key: str) -> str:
    for label, kws in _DOC_TYPE_RULES:
        if any(kw in key for kw in kws):
            return label
    return ""


# 段尾并非人名而是文档类型词时的黑名单(防把"学历证书""统计表"等误当人名)
_NOT_PERSON = (
    "证书", "证明", "统计", "清单", "合同", "发票", "论文", "报告", "截图",
    "执照", "模版", "模板", "费用", "专利", "软著", "审计", "申请", "名单",
    "目录", "汇总", "方法", "系统", "装置", "说明", "预算", "决算",
)


def _extract_person(dirs: list[str], stem: str) -> str:
    """人名挂段尾(`方法名-人名`);离文件最近优先,命中黑名单词则跳过(视为文档类型而非人名)。"""
    for seg in list(reversed(dirs)) + [stem]:
        m = _PERSON.search(seg)
        if m:
            cand = m.group(1)
            if not any(bad in cand for bad in _NOT_PERSON):
                return cand
    return ""


def parse_metadata(rel_key: str) -> dict[str, str]:
    """从知识库相对 key 解析富化字段;字段:project / person / fee_category /
    spent / budget / invoice_amount / doc_type(解析不出的不塞)。"""
    key = (rel_key or "").strip().lstrip("/")
    if not key:
        return {}
    segs = [s for s in key.split("/") if s]
    filename = segs[-1] if segs else ""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    dirs = segs[:-1]  # 目录段(不含文件名)
    out: dict[str, str] = {}

    # project = 顶层分类段(至少要有 顶层/.../文件 的层级才认为有项目归属)
    if len(segs) >= 2:
        out["project"] = segs[0]

    # person:目录段里离文件最近的"-人名"(带黑名单防误伤),否则看文件名 stem
    person = _extract_person(dirs, stem)
    if person:
        out["person"] = person

    # 费用大类 / 实际支出 / 预算上限:任一目录段
    for seg in dirs:
        m = _FEE_DIR.match(seg)
        if m:
            out["fee_category"] = m.group(1)
            out["spent"] = m.group(2)
            out["budget"] = m.group(3)
            break

    # 发票金额:文件名
    m = _INVOICE.search(filename)
    if m:
        out["invoice_amount"] = m.group(1)

    # doc_type:整条 key 关键词粗判
    dt = _guess_doc_type(key)
    if dt:
        out["doc_type"] = dt

    return out
