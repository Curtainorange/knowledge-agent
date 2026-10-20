"""BM25 稀疏检索：混合检索里的 lexical 通道。

## 为什么换掉 `keyword.py` 的重叠率

`score_keyword` 算的是「query 的二字窗口有多大比例出现在文档里」，本质是**二值命中率**：
不看词频、不看这个词有多常见。两条实质缺陷：

- **没有 IDF**：query 里的「数据」「索引」这类在很多条目里都出现的词，与「艾宾浩斯」
  这种只出现一次的词**同权**，区分度完全靠字面碰巧；
- **没有词频饱和与长度归一化**：长条目字多、碰巧命中的窗口多，天然占便宜。

对「卡片盒式个人知识库」这正好是最坏情况——卡片长度差异极大（一行划线 vs 整段笔记）。

BM25 的三个机制一一对上：IDF 抬罕见词、`k1` 让词频饱和（出现 10 次不比 2 次强多少）、
`b` 惩罚长文。所以混合检索的 lexical 通道用它，而不是继续用重叠率。

## 为什么自己写而不装 rank_bm25

项目原则是不引外部检索依赖（前端零依赖、pytest 全 Mock 不触网），BM25 本身也就百来行。
自己写还能把「中文二字窗口」这个分词策略与 `keyword.py` 对齐，替换前后能用同一个
评测集直接对比（`tests/test_retrieval_eval.py`）。

## 分词

中文没有分词器，按「空白切段 → 段内二字窗口」展开：

    数据库索引 范围查询  →  数据 据库 库索 索引 | 范围 围查 查询

**按段展开而不是整串**：整串会在空格处生出「引范」这种跨词窗口，纯噪音。
单字段查询（「树」「熵」）保留该字，否则一个字都召不回。英文/数字按整词（`b+树` → `b` + `树`）。

## 标题加权

标题命中的信号强于正文（与 `keyword.py` 的 0.6/0.4 同向）。这里不拆两个字段做双份统计，
而是把标题 token 的 **tf 乘 `title_boost`**、长度也按加权后算——实现简单，且 BM25 的
饱和项会把「标题词重复」的增益压住，不会变成刷分。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# 连续 ASCII 字母数字整词，或连续中文（含扩展区）串
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75
DEFAULT_TITLE_BOOST = 2.0


def tokenize(text: str) -> list[str]:
    """切词：中文按空白分段后取二字窗口，英文/数字按整词。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer((text or "").lower()):
        chunk = match.group(0)
        if chunk[0].isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


class BM25Index:
    """Okapi BM25 索引（进程内，随写入路径增量维护）。

    - `add` 是覆盖语义（同一 doc_id 先删再加），与 `VectorStore.upsert` 对齐；
    - 打分只遍历 query token 的倒排表，不扫全库：候选集天然受限。
    """

    def __init__(
        self,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
        title_boost: float = DEFAULT_TITLE_BOOST,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.title_boost = title_boost
        self._tf: dict[str, Counter[str]] = {}
        self._len: dict[str, float] = {}
        # token → 含该 token 的文档集合（df 直接取 len）
        self._postings: dict[str, set[str]] = {}
        self._total_len = 0.0

    def __len__(self) -> int:
        return len(self._tf)

    def add(self, doc_id: str, title: str = "", content: str = "") -> None:
        """写入 / 覆盖一条文档。标题与正文都为空时视为不索引（省掉无效统计）。"""
        self.remove(doc_id)
        tf: Counter[str] = Counter()
        for token in tokenize(title):
            tf[token] += self.title_boost
        for token in tokenize(content):
            tf[token] += 1.0
        if not tf:
            return
        length = float(sum(tf.values()))
        self._tf[doc_id] = tf
        self._len[doc_id] = length
        self._total_len += length
        for token in tf:
            self._postings.setdefault(token, set()).add(doc_id)

    def remove(self, doc_id: str) -> None:
        tf = self._tf.pop(doc_id, None)
        if tf is None:
            return
        self._total_len -= self._len.pop(doc_id, 0.0)
        for token in tf:
            docs = self._postings.get(token)
            if docs is None:
                continue
            docs.discard(doc_id)
            if not docs:  # 空集合留着会让后续 df 计算白查一次
                del self._postings[token]

    def score(self, query: str) -> dict[str, float]:
        """返回 {doc_id: BM25 分}，只含得分 > 0 的文档（无重叠即不出现）。"""
        q_tokens = Counter(tokenize(query))
        if not q_tokens or not self._tf:
            return {}

        n = len(self._tf)
        avgdl = (self._total_len / n) or 1.0
        scores: dict[str, float] = {}

        for token, q_count in q_tokens.items():
            docs = self._postings.get(token)
            if not docs:
                continue
            df = len(docs)
            # Lucene 变体：恒正，避免 n 很小时 df ≈ n 导致 idf 变负、分数反向
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for doc_id in docs:
                tf = self._tf[doc_id][token]
                dl = self._len[doc_id]
                norm = self.k1 * (1 - self.b + self.b * dl / avgdl)
                gain = idf * (tf * (self.k1 + 1.0) / (tf + norm)) * q_count
                scores[doc_id] = scores.get(doc_id, 0.0) + gain

        return scores
