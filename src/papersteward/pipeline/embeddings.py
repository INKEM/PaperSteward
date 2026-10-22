"""离线 embedding 边界；哈希实现只用于确定性管线/测试，不代表语义模型。

真实 embedding 供应商需要单独 ADR 与显式授权；本模块不访问网络。余弦相似度只
返回原始值，是否需要分位数/isotonic 校准由调用方决定。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本 embedding；同一 provider 内维度必须稳定。"""

    @property
    def model_id(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class HashingEmbeddingProvider:
    """基于词元哈希的确定性离线 embedding（有符号 bag-of-tokens + L2）。

    英文按单词、中文按单字切分；它捕捉字面重合，不具备跨语言/语义泛化能力。
    """

    def __init__(self, *, dimensions: int = 256, model_id: str = "hashing-v1") -> None:
        if not 8 <= dimensions <= 4096:
            raise ValueError("embedding dimensions 必须在 8–4096 之间")
        if not model_id.strip():
            raise ValueError("embedding model_id 不能为空")
        self._dimensions = dimensions
        self._model_id = model_id.strip()

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if isinstance(texts, str):
            raise TypeError("texts 必须是字符串序列，不是单个字符串")
        if not texts:
            raise ValueError("texts 不能为空")
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("embedding 文本不能为空")
        vector = [0.0] * self._dimensions
        tokens = _TOKEN_RE.findall(text.casefold())
        if not tokens:
            raise ValueError("embedding 文本没有可哈希词元")
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self._dimensions
            sign = -1.0 if (value >> 63) & 1 else 1.0
            vector[index] += sign
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0:
            return tuple(vector)
        return tuple(component / norm for component in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """返回 [-1,1] 余弦；零向量相似度为 0，维度不一致抛 ValueError。"""
    if len(left) != len(right) or not left:
        raise ValueError("embedding 维度必须一致且非空")
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        if not math.isfinite(a) or not math.isfinite(b):
            raise ValueError("embedding 分量必须是有限数")
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / math.sqrt(left_norm * right_norm)))


__all__ = ["EmbeddingProvider", "HashingEmbeddingProvider", "cosine_similarity"]
