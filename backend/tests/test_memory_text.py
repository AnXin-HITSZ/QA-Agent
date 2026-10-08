"""中文词项与 BM25:纯函数测试,不碰网络 / 数据库。

重点不是"实现了一个 BM25",而是这几条口径(§8):
- 中文不能靠空格切(必须有二元组),写与查用同一套规则;
- 分数只在一个语料内可比,排序稳定(同分不许飘);
- 空查询 / 空语料 / 无词项正文不炸。
"""

from __future__ import annotations

from app.memory import text as memory_text


# ---- 词项规则(cjk-bigram-v1) ----


def test_chinese_is_split_into_bigrams_not_by_space():
    assert memory_text.tokenize("报销流程") == ["报销", "销流", "流程"]


def test_single_character_runs_stay_as_their_own_term():
    assert memory_text.tokenize("我 的") == ["我", "的"]        # 单字各自成词,不组对
    assert memory_text.tokenize("A") == ["a"]


def test_ascii_words_are_kept_whole_and_lowercased():
    assert memory_text.tokenize("A4纸 ABC-1 型号") == ["a4", "纸", "abc", "1", "型号"]
    assert memory_text.tokenize("sk-abc123") == ["sk", "abc123"]


def test_full_width_and_case_differences_normalize_to_the_same_terms():
    """写查一致的前提:全角 / 半角、大小写必须先归一,否则同一条记忆查不到。"""
    assert memory_text.tokenize("ＣＳＶ") == memory_text.tokenize("csv")
    assert memory_text.tokenize("LoCoMo") == memory_text.tokenize("locomo")


def test_punctuation_and_whitespace_only_split_never_become_terms():
    assert memory_text.tokenize("实验记录,请用【中文】。") == ["实验", "验记", "记录", "请用", "中文"]
    assert memory_text.tokenize("   ") == []
    assert memory_text.tokenize("") == []
    assert memory_text.tokenize("!!!???...") == []


def test_tokenize_is_deterministic_and_versioned():
    sample = "用户偏好把结果导出为CSV格式"
    assert memory_text.tokenize(sample) == memory_text.tokenize(sample)
    assert memory_text.TOKENIZER_VERSION == "cjk-bigram-v1"     # 改规则必须改版本号
    assert memory_text.terms_digest([sample]) == memory_text.tokenize(sample)


def test_japanese_kana_is_also_bigrammed():
    assert memory_text.tokenize("テスト") == ["テス", "スト"]


# ---- BM25 ----


def _ids(hits) -> list[str]:
    return [h.doc_id for h in hits]


def test_chinese_query_matches_without_any_spaces():
    """query 与正文都没有空格:能命中就说明用的是二元组而不是 split(" ")。"""
    docs = [("m1", "用户偏好用中文写实验记录"), ("m2", "用户喜欢喝咖啡")]
    hits = memory_text.bm25_scores("中文实验记录", docs)
    assert _ids(hits) == ["m1"] and hits[0].score > 0


def test_docs_without_shared_terms_are_not_returned():
    docs = [("m1", "用户偏好用中文"), ("m2", "完全无关的一条")]
    assert _ids(memory_text.bm25_scores("中文", docs)) == ["m1"]


def test_more_matched_terms_rank_higher_and_scores_descend():
    """命中词项多的排前面(取同样长的正文,排除长度归一的干扰)。"""
    docs = [("two", "实验记录"), ("one", "记录"), ("none", "报销")]
    hits = memory_text.bm25_scores("实验记录", docs)
    assert _ids(hits) == ["two", "one"]
    assert hits[0].score > hits[1].score


def test_rare_terms_outweigh_common_ones():
    """IDF 必须起作用:只有一条命中罕用词时,它应当排在「人人都命中」的正文前面。"""
    docs = [("common", "实验"), ("rare", "用户偏好用中文写实验记录")]
    hits = memory_text.bm25_scores("实验记录", docs)
    assert _ids(hits) == ["rare", "common"]
    assert hits[0].score > hits[1].score


def test_scores_are_stable_for_tied_docs():
    """同分不许飘:按 doc_id 兜底排序,否则每次检索顺序都可能不一样。"""
    docs = [("b", "实验记录"), ("a", "实验记录")]
    assert _ids(memory_text.bm25_scores("实验记录", docs)) == ["a", "b"]
    assert _ids(memory_text.bm25_scores("实验记录", list(reversed(docs)))) == ["a", "b"]


def test_longer_docs_are_length_normalized():
    """长度归一(b)要起作用:关键词密度高的短正文不该被长正文压过去。"""
    long_doc = "实验记录" + "填充" * 60
    docs = [("long", long_doc), ("short", "实验记录")]
    hits = memory_text.bm25_scores("实验记录", docs)
    assert hits[0].doc_id == "short"


def test_hit_reports_matched_terms_but_not_full_text():
    hits = memory_text.bm25_scores("中文记录", [("m1", "用户偏好用中文写实验记录")])
    assert "中文" in hits[0].matched and "记录" in hits[0].matched
    assert all(len(t) <= 4 for t in hits[0].matched)


def test_empty_inputs_return_no_hits():
    assert memory_text.bm25_scores("", [("m1", "实验记录")]) == []
    assert memory_text.bm25_scores("   ", [("m1", "实验记录")]) == []
    assert memory_text.bm25_scores("实验记录", []) == []
    assert memory_text.bm25_scores("实验记录", [("m1", "!!!")]) == []   # 正文没有词项


def test_query_terms_are_deduplicated():
    """查询里重复的词只贡献一次,否则「实验实验实验」会把分数刷成三倍。"""
    docs = [("m1", "实验记录")]
    once = memory_text.bm25_scores("实验记录", docs)[0].score
    thrice = memory_text.bm25_scores("实验实验实验记录记录记录", docs)[0].score
    assert once == thrice
