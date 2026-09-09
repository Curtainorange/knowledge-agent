"""Embedding 模型抽象（D1）。

DeepSeek 无标准 embedding 接口，因此向量来源独立选址。此处用抽象收口，
默认走本地 BGE（真语义），测试/离线走确定性哈希 embedding；swap/换 pgvector
都不改动上层 L1 逻辑。

铁律：真实模型惰性构造（首次真正调用才加载/下载），且加载失败可降级，
不阻碍业务主链路（与可靠-4 一致）。
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from abc import ABC, abstractmethod
from typing import ClassVar

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingModel(ABC):
    """统一 embedding 契约。"""

    dim: ClassVar[int]

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量把文本映射为定长向量（每行一个）。"""

    # 向量序列化：MVP 存关系库 BLOB，检索时按需加载
    @staticmethod
    def dumps(vec: list[float]) -> bytes:
        return pickle.dumps([float(x) for x in vec])

    @staticmethod
    def loads(data: bytes) -> list[float]:
        return pickle.loads(data)


class BGEHesEmbedding(EmbeddingModel):
    """本地 BGE（BAAI/bge-small-zh-v1.5），ONNX 轻量真语义。

    惰性加载：首次 embed 才实例化 fastembed.TextEmbedding（可能触发一次模型下载）。
    """

    dim = 512

    def __init__(self) -> None:
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                raise RuntimeError(
                    "embedding_backend=bge 需要 fastembed（pip install fastembed）"
                ) from exc
            logger.info("lazy-loading local embedding model %s ...", settings.embedding_model)
            self._model = TextEmbedding(model_name=settings.embedding_model)
            self.dim = len(self._model.embed(["测试"])[0]) or self.dim
            logger.info("local embedding model ready (dim=%d)", self.dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


class HashEmbedding(EmbeddingModel):
    """确定性、零依赖的哈希 embedding（测试/离线兜底）。

    字符 n-gram（含中文）哈希到大整数 → 符号 → 归一化向量。词法近似，语义弱，
    仅用于离线单测与无网环境，保证 pytest 全程不触网、不装大模型。
    """

    dim = 256

    def __init__(self, n_gram: int = 3, n_hashes: int = 1) -> None:
        self.n_gram = n_gram
        self.n_hashes = n_hashes

    @staticmethod
    def _ngrams(text: str, n: int):
        return [text[i:i + n] for i in range(max(0, len(text) - n + 1))]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = np.zeros(self.dim, dtype=float)
            grams = self._ngrams(text.strip(), self.n_gram) or ["*empty*"]
            for gram in grams:
                for h in range(self.n_hashes):
                    digest = hashlib.sha256(f"{h}:{gram}".encode("utf-8")).digest()
                    idx = int.from_bytes(digest[:8], "big") % self.dim
                    sign = 1.0 if digest[8] % 2 == 0 else -1.0
                    vec[idx] += sign
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append((vec / norm).tolist())
        return out


def build_embedding(backend: str | None = None) -> EmbeddingModel:
    """按配置构造 embedding 实现（工厂）。"""
    backend = backend or settings.embedding_backend
    if backend == "hash":
        return HashEmbedding()
    if backend == "bge":
        return BGEHesEmbedding()
    raise ValueError(f"未知 embedding_backend: {backend!r}")