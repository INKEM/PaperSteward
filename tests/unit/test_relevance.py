from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import DocumentStatus, OaStatus, RelevanceBand
from papersteward.documents.profile import parse_profile
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.relevance import (
    RelevanceError,
    band_for_r,
    build_paper_text,
    build_profile_block,
    compute_r,
    evaluate_relevance,
    extract_profile_facets,
    score_rubric,
    score_semantic,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "relevance"
PROFILE_TEXT = (ROOT / "tests" / "fixtures" / "profiles" / "basic.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)

PAPER = Paper(
    id="rel-001",
    title="Long-document retrieval with cross-segment reranking",
    abstract=(
        "We study long-document retrieval and cross-segment reasoning. "
        "Dense retrieval and cross-encoder reranking improve retrieval on "
        "long-document QA benchmarks. RAG grounding reduces hallucination."
    ),
    oa_status=OaStatus.OPEN,
)


class FixedEmbeddingProvider:
    model_id = "fixed-v1"

    def embed(self, texts):
        return tuple((1.0, 0.0) for _ in texts)


def _profile():
    profile = parse_profile(PROFILE_TEXT, source="basic.md", document_id="demo-profile")
    return replace(profile, status=DocumentStatus.CONFIRMED, confirmed_at=NOW)


def _gateway(response: str) -> LLMGateway:
    return LLMGateway(FakeLLM([response], model_id="fake-llm"), clock=lambda: NOW)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_golden_r_formula_and_bands():
    golden = json.loads(_fixture("golden.json"))

    assert compute_r(golden["semantic"], golden["weighted_rubric"]) == golden["expected_r"]
    assert band_for_r(54.9) is RelevanceBand.BELOW_THRESHOLD
    assert band_for_r(55.0) is RelevanceBand.LOW
    assert band_for_r(64.9) is RelevanceBand.LOW
    assert band_for_r(65.0) is RelevanceBand.NORMAL
    with pytest.raises(RelevanceError, match="band"):
        band_for_r(101.0)


def test_profile_facets_and_prompt_block_are_stable():
    facets = extract_profile_facets(_profile())

    ids = [facet.facet_id for facet in facets]
    assert ids[:3] == ["positioning", "question-Q1", "question-Q2"]
    assert "subdirection-S1" in ids and "methods-1" in ids
    assert any("long-document retrieval" in facet.text for facet in facets)
    block = build_profile_block(_profile())
    assert "核心问题" in block and "子方向" in block and "methods" in block
    assert build_paper_text(PAPER).startswith(PAPER.title)


def test_empty_profile_facets_and_draft_profile_are_rejected():
    profile = _profile()
    with pytest.raises(RelevanceError, match="profile"):
        extract_profile_facets(replace(profile, parsed_json={}))
    with pytest.raises(RelevanceError, match="已确认"):
        evaluate_relevance(
            replace(profile, status=DocumentStatus.DRAFT, confirmed_at=None),
            PAPER,
            _gateway(_fixture("rubric.valid.json")),
            FixedEmbeddingProvider(),
        )


def test_valid_rubric_with_fixed_semantic_scores_exact_r():
    gateway = _gateway(_fixture("rubric.valid.json"))
    result = evaluate_relevance(_profile(), PAPER, gateway, FixedEmbeddingProvider())

    assert result.semantic.score == 1.0
    assert result.rubric.weighted_score == pytest.approx(4.6)
    assert result.R == 95.2
    assert result.band is RelevanceBand.NORMAL
    assert result.needs_review is False
    assert result.rubric.missing_dimensions == ()
    assert result.rubric.invalid_quotes == ()
    assert all(item.valid for item in result.rubric.evidence)
    assert result.provenance.model == "fake-llm"
    assert result.provenance.prompt_version == "relevance/v2"
    assert result.reasons == ("uncalibrated_similarity",)
    assert "不可信数据" in gateway.provider.calls[0]
    assert "研究档案" in gateway.provider.calls[0]


def test_missing_evidence_uses_neutral_score_and_requires_review():
    result = evaluate_relevance(
        _profile(),
        PAPER,
        _gateway(_fixture("rubric.missing_evidence.json")),
        FixedEmbeddingProvider(),
    )

    assert result.rubric.missing_dimensions == ("actionability",)
    assert result.rubric.effective_scores["actionability"] == 2.5
    assert result.rubric.weighted_score == pytest.approx(4.375)
    assert result.R == 92.5
    assert result.needs_review is True
    assert "missing_evidence:actionability" in result.reasons


def test_fabricated_evidence_is_invalid_and_routes_to_review():
    result = evaluate_relevance(
        _profile(),
        PAPER,
        _gateway(_fixture("rubric.fabricated_evidence.json")),
        FixedEmbeddingProvider(),
    )

    assert result.rubric.missing_dimensions == ("problem",)
    assert result.rubric.invalid_quotes == ("a quote not present in the paper",)
    assert result.rubric.effective_scores["problem"] == 2.5
    assert result.needs_review is True
    assert "invalid_evidence" in result.reasons
    assert "missing_evidence:problem" in result.reasons


def test_low_rubric_confidence_requires_review():
    result = evaluate_relevance(
        _profile(),
        PAPER,
        _gateway(_fixture("rubric.low_confidence.json")),
        FixedEmbeddingProvider(),
    )

    assert result.rubric.missing_dimensions == ()
    assert result.needs_review is True
    assert "low_rubric_confidence" in result.reasons


def test_missing_abstract_invalidates_abstract_quotes_and_reviews():
    paper = replace(PAPER, abstract=None)
    result = evaluate_relevance(
        _profile(), paper, _gateway(_fixture("rubric.valid.json")), FixedEmbeddingProvider()
    )

    assert result.rubric.missing_dimensions == ("method", "domain", "actionability")
    assert result.needs_review is True
    assert "missing_abstract" in result.reasons


def test_semantic_calibrator_and_hashing_provider_end_to_end():
    profile = _profile()
    facets = extract_profile_facets(profile)
    hashing = HashingEmbeddingProvider(dimensions=128)

    raw = score_semantic(PAPER, facets, hashing)
    assert 0.0 <= raw.score <= 1.0 and raw.calibrated is False
    calibrated = score_semantic(PAPER, facets, hashing, calibrator=lambda _: 0.5)
    assert calibrated.score == 0.5 and calibrated.calibrated is True
    with pytest.raises(RelevanceError, match="校准"):
        score_semantic(PAPER, facets, hashing, calibrator=lambda _: 2.0)

    result = evaluate_relevance(profile, PAPER, _gateway(_fixture("rubric.valid.json")), hashing)
    payload = result.as_json()
    assert 0.0 <= result.R <= 100.0
    assert result.semantic.model_id == "hashing-v1"
    assert payload["semantic"]["calibrated"] is False
    assert payload["rubric"]["scores"]["problem"] == 5


def test_invalid_semantic_inputs_and_provider_shape():
    profile = _profile()
    facets = extract_profile_facets(profile)

    class BrokenProvider:
        model_id = "broken"

        def embed(self, texts):
            return ((1.0,),)

    with pytest.raises(RelevanceError, match="数量或维度"):
        score_semantic(PAPER, facets, BrokenProvider())
    with pytest.raises(RelevanceError, match="片段"):
        score_semantic(PAPER, (), HashingEmbeddingProvider())


def test_invalid_min_confidence_is_rejected_before_llm_call():
    gateway = _gateway(_fixture("rubric.valid.json"))

    with pytest.raises(RelevanceError, match="min_confidence"):
        score_rubric(gateway, _profile(), PAPER, min_confidence=2.0)

    assert gateway.provider.calls == []
