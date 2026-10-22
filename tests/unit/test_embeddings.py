from __future__ import annotations

import math

import pytest

from papersteward.pipeline.embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    cosine_similarity,
)


def test_hashing_embedding_is_deterministic_normalized_and_bounded():
    provider = HashingEmbeddingProvider(dimensions=64)

    first = provider.embed(["long-document retrieval", "long-document retrieval"])
    second = provider.embed(["long-document retrieval"])

    assert isinstance(provider, EmbeddingProvider)
    assert first[0] == first[1] == second[0]
    assert len(first[0]) == 64
    assert sum(component * component for component in first[0]) == pytest.approx(1.0)
    assert all(math.isfinite(component) for component in first[0])


def test_hashing_similarity_prefers_overlapping_text():
    provider = HashingEmbeddingProvider(dimensions=128)
    base, close, far = provider.embed(
        [
            "long-document retrieval with cross-encoder reranking",
            "long-document retrieval and reranking",
            "protein folding with cryo-electron microscopy",
        ]
    )

    assert cosine_similarity(base, base) == pytest.approx(1.0)
    assert cosine_similarity(base, close) > cosine_similarity(base, far)
    # 哈希碰撞可能带来少量偶然重合；只要求远文本显著低于近文本。
    assert cosine_similarity(base, far) < 0.3


def test_cosine_handles_orthogonal_zero_and_invalid_vectors():
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == 0.0
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
    assert cosine_similarity((-1.0, 0.0), (1.0, 0.0)) == pytest.approx(-1.0)

    with pytest.raises(ValueError, match="维度"):
        cosine_similarity((1.0,), (1.0, 0.0))
    with pytest.raises(ValueError, match="有限"):
        cosine_similarity((float("nan"), 0.0), (1.0, 0.0))


@pytest.mark.parametrize(
    "kwargs",
    [{"dimensions": 7}, {"dimensions": 4097}, {"model_id": "   "}],
)
def test_invalid_embedding_constructor(kwargs):
    with pytest.raises(ValueError):
        HashingEmbeddingProvider(**kwargs)


@pytest.mark.parametrize("texts", [[], ["   "], ["!!!"], "single-string"])
def test_invalid_embedding_input(texts):
    provider = HashingEmbeddingProvider(dimensions=32)
    with pytest.raises((TypeError, ValueError)):
        provider.embed(texts)  # type: ignore[arg-type]
