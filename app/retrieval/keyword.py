"""关键词兜底检索（对齐可靠-4：向量不可用降级关键词）。

打分规则：标题命中权重最高，正文命中次之，采用字符窗口 n-gram 重叠，
对中文亦有效。纯确定性，无外部依赖。
"""
from __future__ import annotations


def _ngrams_set(text: str, n: int) -> set[str]:
    t = (text or "").strip().lower()
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def score_keyword(query: str, title: str, content: str, *, n: int = 2) -> float:
    """返回 query 相对条目（标题+正文）的关键词重叠得分。"""
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    q_grams = _ngrams_set(q, n)
    if not q_grams:
        return 0.0

    title_grams = _ngrams_set(title, n)
    content_grams = _ngrams_set(content, n)

    title_hit = len(q_grams & title_grams) / len(q_grams)
    content_hit = len(q_grams & content_grams) / len(q_grams)
    # 标题权重 0.6、正文 0.4；完全无重叠则为 0
    return 0.6 * title_hit + 0.4 * content_hit