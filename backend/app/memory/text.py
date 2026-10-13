"""中文词项处理与 BM25 打分(纯函数,不碰网络 / 数据库):记忆的关键词召回。

两条通道共用同一套词项规则:
- **主通道**:写入时把正文编成 Qdrant 的稀疏向量(bm25),检索时把查询编成同一个空间的
  稀疏向量交给 Qdrant(服务端用 Modifier.IDF 乘 IDF)——见 sparse_terms / SPARSE_*;
- **降级通道**:Qdrant / Embedding 不可用时,用 bm25_scores 在「当前用户当前作用域的
  有效正文」上现算(见 search.py),显式标注为降级。

为什么自己做而不用第三方分词:
- 不新增依赖(项目只允许既有那一套:MySQL / Qdrant / Redis / DashScope);
- 记忆正文很短(默认 ≤800 字),BM25 的语料就是**当前用户的有效记忆**,所以按用户现取
  即可,不需要全库词频也不需要倒排落库;
- 规则必须**写与查完全一致**,且可版本化:TOKENIZER_VERSION 变了就等于口径变了
  (与 embedding_version 同一类语义,换分词规则应当重建 / 至少记录在案)。

词项规则(TOKENIZER_VERSION = cjk-bigram-v1):
- NFKC 归一 + 大小写折叠(全角字母 / 数字与半角等价,写查一致);
- 连续汉字(含扩展 A 区与假名)切**二元组**(bigram),单字独立成词 —— 中文没有空格,
  绝不能靠 split(" ")(§8);二元组是免词典分词里召回与噪声比较平衡的做法;
- 拉丁字母 / 数字 / 下划线的连续串作一个词(小写);
- 其余字符(标点 / 空白 / 表情)一律丢弃,只做分割。

BM25 用标准公式(与 Lucene 同形):
    IDF(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
    score(D, Q) = Σ_t IDF(t) · f(t,D)·(k1+1) / (f(t,D) + k1·(1 - b + b·|D|/avgdl))
IDF 由**本用户本次语料**现算(N 就是语料条数):记忆是私有数据,不存在跨用户共享词频;
每次检索重算的量级是「用户记忆条数 × 平均词项数」,在语料上限(见 search.py)内可控。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

# 分词规则版本:改动分词 / 归一规则时必须 +1(检索口径变了,旧索引应当重建)
TOKENIZER_VERSION = "cjk-bigram-v1"

# BM25 参数(常用默认;记忆正文短,长度归一几乎不起作用,但保留 b 以免长文主导)
K1 = 1.2
B = 0.75

# 稀疏索引口径版本:词项编码方式 / 权重公式 / 服务端 IDF 设置变了就 +1
# (稀疏向量随 embedding_version 一起失效重建,见 vector.index_version)
SPARSE_INDEX_VERSION = "qdrant-sparse-bm25-idf-v1"

# 汉字(基本区 U+4E00-9FFF / 扩展 A U+3400-4DBF / 兼容区 U+F900-FAFF)+ 假名 U+3040-30FF
_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ"
_TOKEN_RE = re.compile(f"([{_CJK}]+|[0-9a-z_]+)")


def tokenize(text: str) -> list[str]:
    """正文 / 查询共用的词项切分(顺序保留,重复保留 —— 词频由调用方统计)。"""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        elif run[0].isascii():          # 拉丁 / 数字串:整串一个词
            tokens.append(run)
        else:                            # 汉字 / 假名:二元组
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def terms_digest(texts: Sequence[str]) -> list[str]:
    """把一批正文切成词项(语义化别名,便于阅读调用点)。"""
    return tokenize(" ".join(texts))


# ---- 稀疏向量编码(Qdrant 的 bm25 命名向量)----
#
# Qdrant 的稀疏向量是「下标 → 权重」的稀疏 map,下标必须是非负整数、同一向量内唯一。
# 这里把词项哈希成 32 位无符号整数当下标(哈希碰撞会把两个词算成一个,代价是极少数
# 词项权重相加 —— 对短正文的召回影响可忽略,换来的是不需要维护词表)。
# 权重只做**词频饱和**:f·(k1+1)/(f+k1);长度的那一项(b 与 avgdl)服务端拿不到语料
# 统计量,故不参与 —— 这是与经典 BM25 的唯一偏差,记在 SPARSE_INDEX_VERSION 里:
# 参数一改版本就变,索引随之整体失效重建,不会「旧权重配新公式」。
# IDF 由 Qdrant 按集合内文档频率现算(Modifier.IDF),与降级通道的公式同形。

_TERM_HASH_SIZE = 4          # 32 位下标


def term_index(term: str) -> int:
    """词项 → 稀疏向量的下标(稳定哈希:同一词项在任何进程 / 任何时间都是同一个下标)。"""
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=_TERM_HASH_SIZE).digest()
    return int.from_bytes(digest, "big") % (2 ** 32)


def sparse_terms(text: str, *, k1: float = K1) -> tuple[list[int], list[float]]:
    """正文 → 稀疏向量(下标升序、下标唯一);没有词项时返回空。

    写与查**必须是同一个函数**:查询侧用同一个编码,否则「索引里有、查询命中不到」。
    """
    counts: dict[int, int] = {}
    for token in tokenize(text):
        idx = term_index(token)
        counts[idx] = counts.get(idx, 0) + 1
    if not counts:
        return [], []
    indices = sorted(counts)
    weights = [counts[i] * (k1 + 1.0) / (counts[i] + k1) for i in indices]
    return indices, weights


def index_version() -> str:
    """稀疏 + 分词口径的版本串(拼进 embedding_version:口径变了索引整体失效重建)。"""
    return f"{TOKENIZER_VERSION}:{SPARSE_INDEX_VERSION}:k1={K1}"


@dataclass(frozen=True)
class Bm25Hit:
    """一条关键词命中的打分结果(分数只在本次语料内可比,跨语料无意义)。"""

    doc_id: str
    score: float
    matched: tuple[str, ...]      # 命中的词项(诊断用,不含正文)


def bm25_scores(query: str, docs: Iterable[tuple[str, str]], *, k1: float = K1,
                b: float = B) -> list[Bm25Hit]:
    """按 BM25 给语料打分,返回分数 > 0 的结果(降序;同分按 doc_id 稳定排序)。

    docs 是 (id, 正文) 序列;语料由调用方按用户取好(见 search.py 的有上限取法)。
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    q_terms = list(dict.fromkeys(q_tokens))          # 查询里重复的词只贡献一次

    doc_freq: dict[str, int] = {}
    rows: list[tuple[str, dict[str, int], int]] = []
    for doc_id, text in docs:
        counts: dict[str, int] = {}
        for t in tokenize(text):
            counts[t] = counts.get(t, 0) + 1
        if not counts:
            continue
        rows.append((doc_id, counts, sum(counts.values())))
        for t in counts:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    n = len(rows)
    if not n:
        return []
    avgdl = sum(length for _, _, length in rows) / n

    hits: list[Bm25Hit] = []
    for doc_id, counts, length in rows:
        total = 0.0
        matched: list[str] = []
        for term in q_terms:
            f = counts.get(term)
            if not f:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            total += idf * (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * length / avgdl))
            matched.append(term)
        if total > 0:
            hits.append(Bm25Hit(doc_id=doc_id, score=total, matched=tuple(matched)))
    hits.sort(key=lambda h: (-h.score, h.doc_id))
    return hits
