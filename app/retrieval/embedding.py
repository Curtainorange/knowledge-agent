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
import os
import pickle
import time
from abc import ABC, abstractmethod
from pathlib import Path
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

    加载失败后进入冷却期、期间直接快速失败：否则每次录入都会重新尝试下载并卡住
    数十秒，把请求线程和数据库写锁一起拖住（实测会连锁引发 "database is locked"）。
    """

    dim = 512

    def __init__(self, retry_cooldown_seconds: float = 300.0) -> None:
        self._model = None
        self._failed_at: float | None = None
        self._cooldown = retry_cooldown_seconds

    def _ensure(self):
        if self._model is not None:
            return
        if self._failed_at is not None:
            waited = time.monotonic() - self._failed_at
            if waited < self._cooldown:
                raise RuntimeError(
                    f"向量模型此前加载失败，{int(self._cooldown - waited)} 秒内不再重试"
                    "（避免每次录入都卡在下载上）"
                )
            self._failed_at = None  # 冷却结束，允许再试一次

        # 国内走 HF 镜像（config 优先级低于已有环境变量，避免覆盖 shell 显式配置）
        if settings.hf_endpoint and not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = settings.hf_endpoint
        if settings.hf_hub_disable_xet:
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "embedding_backend=bge 需要 fastembed（pip install fastembed）"
            ) from exc

        logger.info("lazy-loading local embedding model %s ...", settings.embedding_model)
        try:
            self._model = TextEmbedding(model_name=settings.embedding_model)
            self.dim = len(list(self._model.embed(["测试"]))[0]) or self.dim
        except Exception:
            self._failed_at = time.monotonic()
            logger.warning("本地向量模型加载失败，%d 秒内不再重试", int(self._cooldown))
            raise
        logger.info("local embedding model ready (dim=%d)", self.dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


class LocalOnnxEmbedding(EmbeddingModel):
    """从本地目录加载 ONNX 模型（不依赖任何模型源，可完全离线）。

    适用场景：`huggingface.co` / `hf-mirror.com` 不可达（DNS 能解析但 TCP 连不上），
    此时 fastembed 无法自动下载模型。用 `scripts/fetch_embedding_model.py` 预取到本地，
    之后长期可用、不再联网。

    模型若已输出 `sentence_embedding` 就直接取用，否则回退 CLS 池化；最后统一做
    L2 归一化（BGE 官方推荐用法）。
    """

    def __init__(self, model_dir: str | None = None, max_length: int = 512) -> None:
        self._dir = Path(model_dir or settings.embedding_local_dir)
        self._max_length = max_length
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()
        self._output_names: list[str] = []

    def _resolve_weight(self) -> Path:
        for name in ("model_quantized.onnx", "model.onnx"):
            candidate = self._dir / "onnx" / name
            if candidate.exists():
                return candidate
        raise RuntimeError(
            f"本地模型缺少 onnx 权重：{self._dir / 'onnx'}。"
            "请先执行 python scripts/fetch_embedding_model.py"
        )

    def _ensure(self):
        if self._session is not None:
            return
        tokenizer_path = self._dir / "tokenizer.json"
        if not tokenizer_path.exists():
            raise RuntimeError(
                f"本地模型缺少 tokenizer.json：{self._dir}。"
                "请先执行 python scripts/fetch_embedding_model.py"
            )
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "embedding_backend=local 需要 onnxruntime 与 tokenizers（随 fastembed 安装）"
            ) from exc

        weight = self._resolve_weight()
        logger.info("loading local onnx embedding model from %s ...", weight)
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=self._max_length)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        session = ort.InferenceSession(str(weight), providers=["CPUExecutionProvider"])
        self._tokenizer = tokenizer
        self._session = session
        self._input_names = {item.name for item in session.get_inputs()}
        self._output_names = [item.name for item in session.get_outputs()]
        for output in session.get_outputs():
            if output.name == "sentence_embedding" and output.shape:
                self.dim = int(output.shape[-1])
        logger.info("local onnx model ready (dim=%d)", self.dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        encodings = self._tokenizer.encode_batch(list(texts))
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)

        outputs = self._session.run(None, feed)
        if "sentence_embedding" in self._output_names:
            vectors = outputs[self._output_names.index("sentence_embedding")]
        else:
            vectors = outputs[0][:, 0]  # 模型未提供池化输出时退回 CLS

        vectors = np.asarray(vectors, dtype=float)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (vectors / norms).tolist()


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


_instances: dict[str, EmbeddingModel] = {}


def build_embedding(backend: str | None = None) -> EmbeddingModel:
    """按配置构造 embedding 实现（工厂）。

    同 backend 复用同一实例：模型的加载状态（尤其是**加载失败的冷却期**）保存在
    实例上，若每个请求都新建实例，冷却就永远不生效 —— 表现为每次录入都重新尝试
    下载并卡住数十秒。
    """
    backend = backend or settings.embedding_backend
    if backend not in _instances:
        if backend == "hash":
            _instances[backend] = HashEmbedding()
        elif backend == "local":
            _instances[backend] = LocalOnnxEmbedding()
        elif backend == "bge":
            _instances[backend] = BGEHesEmbedding()
        else:
            raise ValueError(f"未知 embedding_backend: {backend!r}")
    return _instances[backend]


def reset_embedding_cache() -> None:
    """清空实例缓存（仅供测试切换后端 / 重置加载状态）。"""
    _instances.clear()