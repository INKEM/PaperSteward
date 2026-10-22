from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from papersteward.core.entities import (
    EnrichmentSignals,
    NominationEvidence,
    Paper,
    Provenance,
    SpecialNominationRecord,
)
from papersteward.core.enums import (
    OaStatus,
    ScoreTier,
    SpecialBucketKind,
    SpecialNominationStatus,
)
from papersteward.scoring.special import (
    HIGH_TIER_MIN,
    MEDIUM_TIER_MIN,
    RUBRIC_WEIGHTS,
    SpecialTierError,
    assess_nomination,
    compute_special_value,
    tier_for_value,
)

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
PROVENANCE = Provenance("fixture", "special_nomination/v1", NOW, "0" * 64)


def _nomination(
    kind: SpecialBucketKind,
    *,
    confidence: float = 0.82,
    evidence: tuple[tuple[str, bool], ...] = (("Paragraph clustering recall", True),),
    review_layers: tuple[str, ...] = ("method", "evaluation"),
    classic_basis: tuple[str, ...] = (),
    bridge_memo: dict[str, str] | None = None,
    reasons: tuple[str, ...] = (),
) -> SpecialNominationRecord:
    return SpecialNominationRecord(
        id=f"nom-{kind.value}",
        paper_id="p1",
        kind=kind,
        status=SpecialNominationStatus.ACCEPTED,
        confidence=confidence,
        reason="合成提名",
        evidence=tuple(NominationEvidence(quote, valid) for quote, valid in evidence),
        review_layers=review_layers,
        classic_basis=classic_basis,
        bridge_memo=bridge_memo,
        reasons=reasons,
        provenance=PROVENANCE,
        nominated_at=NOW,
        decided_at=NOW,
        decided_by="fixture-user",
    )


def _paper(**kwargs) -> Paper:
    return Paper(id="p1", title="Synthetic paper", abstract="Synthetic abstract", **kwargs)


def test_rubric_weights_are_frozen_and_complete() -> None:
    assert set(RUBRIC_WEIGHTS) == set(SpecialBucketKind)
    assert set(RUBRIC_WEIGHTS[SpecialBucketKind.REVIEW]) == {
        "cross_layer_depth",
        "coverage",
        "authority",
        "clarity",
        "timeliness",
    }
    assert set(RUBRIC_WEIGHTS[SpecialBucketKind.DISTANT]) == {
        "transfer_potential",
        "bridge_clarity",
        "credibility",
        "novelty",
        "accessibility",
    }
    assert set(RUBRIC_WEIGHTS[SpecialBucketKind.CLASSIC]) == {
        "foundational",
        "long_term_impact",
        "authority",
        "current_relevance",
    }
    for weights in RUBRIC_WEIGHTS.values():
        assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)


@pytest.mark.parametrize(
    ("value", "tier"),
    [
        (100.0, ScoreTier.HIGH),
        (75.0, ScoreTier.HIGH),
        (74.9, ScoreTier.MEDIUM),
        (50.0, ScoreTier.MEDIUM),
        (49.9, ScoreTier.LOW),
        (0.0, ScoreTier.LOW),
        (74.95, ScoreTier.HIGH),
    ],
)
def test_tier_thresholds_are_table_driven(value: float, tier: ScoreTier) -> None:
    assert HIGH_TIER_MIN == 75.0
    assert MEDIUM_TIER_MIN == 50.0
    assert tier_for_value(value) is tier


@pytest.mark.parametrize("value", [-0.1, 100.1, float("nan"), True])
def test_tier_value_rejects_invalid_numbers(value: object) -> None:
    with pytest.raises(SpecialTierError):
        tier_for_value(value)  # type: ignore[arg-type]


def test_weighted_value_and_contributions_are_deterministic() -> None:
    review = compute_special_value(
        SpecialBucketKind.REVIEW,
        {
            "cross_layer_depth": 80,
            "coverage": 70,
            "authority": 60,
            "clarity": 90,
            "timeliness": 50,
        },
    )
    assert review.value == 72.0
    assert review.contributions["cross_layer_depth"] == 24.0

    distant = compute_special_value(
        SpecialBucketKind.DISTANT,
        {
            "transfer_potential": 90,
            "bridge_clarity": 80,
            "credibility": 70,
            "novelty": 60,
            "accessibility": 50,
        },
    )
    assert distant.value == 75.5
    assert tier_for_value(distant.value) is ScoreTier.HIGH

    classic = compute_special_value(
        SpecialBucketKind.CLASSIC,
        {
            "foundational": 90,
            "long_term_impact": 80,
            "authority": 70,
            "current_relevance": 60,
        },
    )
    assert classic.value == 77.5


def test_assessment_rejects_non_frozen_weights() -> None:
    result = assess_nomination(
        _nomination(SpecialBucketKind.REVIEW),
        _paper(),
        as_of=NOW.date(),
    )
    bad = dict(result.weights)
    bad["authority"] = 0.21
    bad["clarity"] = 0.14
    with pytest.raises(SpecialTierError, match="冻结"):
        replace(result, weights=bad)


def test_dimension_validation_rejects_missing_keys_and_out_of_range() -> None:
    with pytest.raises(SpecialTierError, match="维度"):
        compute_special_value(SpecialBucketKind.REVIEW, {"authority": 50})
    with pytest.raises(SpecialTierError, match="0–100"):
        compute_special_value(
            SpecialBucketKind.CLASSIC,
            {
                "foundational": 101,
                "long_term_impact": 50,
                "authority": 50,
                "current_relevance": 50,
            },
        )


def test_review_with_strong_signals_reaches_high_tier() -> None:
    nomination = _nomination(
        SpecialBucketKind.REVIEW,
        confidence=0.9,
        evidence=(("Paragraph clustering recall", True), ("cross-layer attention", True)),
        review_layers=("problem", "method", "evaluation"),
    )
    enrichment = EnrichmentSignals(
        venue_level="a",
        citation_count=1000,
        oa_status=OaStatus.OPEN,
    )
    result = assess_nomination(
        nomination,
        _paper(published_at=NOW - timedelta(days=400)),
        enrichment=enrichment,
        relevance=70,
        as_of=NOW.date(),
    )
    assert result.value == 92.8
    assert result.tier is ScoreTier.HIGH
    assert result.dimensions["cross_layer_depth"] == 90.0
    assert result.dimensions["authority"] == 81.0
    assert "method_evaluation_bridge" in result.reasons
    assert "venue:a" in result.reasons


def test_review_missing_signals_stay_neutral() -> None:
    result = assess_nomination(
        _nomination(
            SpecialBucketKind.REVIEW,
            confidence=0.5,
            evidence=(("Paragraph clustering recall", True),),
            review_layers=("method", "evaluation"),
        ),
        _paper(),
        as_of=NOW.date(),
    )
    assert result.tier is ScoreTier.MEDIUM
    assert result.dimensions["authority"] == 50.0
    assert result.dimensions["timeliness"] == 50.0
    for reason in ("unknown_venue", "missing_citation", "missing_age"):
        assert reason in result.reasons


def test_distant_uses_bridge_memo_relevance_and_accessibility() -> None:
    nomination = _nomination(
        SpecialBucketKind.DISTANT,
        confidence=0.7,
        review_layers=(),
        bridge_memo={
            "source_mechanism": "门控缓解过平滑。",
            "shared_structure": "跨段依赖是稀疏图消息传递。",
            "transfer_experiment": "在重排器中加入门控并比较召回。",
            "falsification_signal": "若提升仅来自参数量则不成立。",
        },
    )
    enrichment = EnrichmentSignals(
        venue_level="b",
        oa_status=OaStatus.OPEN,
        code_url="https://example.com/code",
        data_url="https://example.com/data",
    )
    result = assess_nomination(
        nomination,
        _paper(oa_status=OaStatus.OPEN),
        enrichment=enrichment,
        relevance=55,
        as_of=NOW.date(),
    )
    assert result.tier is ScoreTier.HIGH
    assert result.dimensions["accessibility"] == 100.0
    assert result.dimensions["novelty"] == 100.0
    assert "bridge_fields:4" in result.reasons
    assert "relevance_distant_band" in result.reasons
    assert result.explanation


def test_distant_missing_bridge_fields_and_low_relevance_are_marked() -> None:
    result = assess_nomination(
        _nomination(
            SpecialBucketKind.DISTANT,
            confidence=0.4,
            review_layers=(),
            bridge_memo={"source_mechanism": "only one"},
            reasons=("missing_bridge_memo",),
        ),
        _paper(),
        relevance=20,
        as_of=NOW.date(),
    )
    assert result.dimensions["accessibility"] == 50.0
    assert "missing_bridge:shared_structure" in result.reasons
    assert "relevance_out_of_band" in result.reasons
    assert "gate_reasons:1" in result.reasons


def test_classic_uses_basis_longevity_and_current_relevance() -> None:
    result = assess_nomination(
        _nomination(
            SpecialBucketKind.CLASSIC,
            confidence=0.9,
            review_layers=(),
            classic_basis=("foundational",),
        ),
        _paper(published_at=NOW - timedelta(days=3650)),
        enrichment=EnrichmentSignals(venue_level="a", citation_count=5000),
        relevance=80,
        as_of=NOW.date(),
    )
    assert result.tier is ScoreTier.HIGH
    assert result.dimensions["foundational"] == 100.0
    assert result.dimensions["long_term_impact"] == 97.4
    assert result.dimensions["current_relevance"] == 80.0
    assert "classic_basis:foundational" in result.reasons


def test_classic_without_basis_is_penalized() -> None:
    result = assess_nomination(
        _nomination(
            SpecialBucketKind.CLASSIC,
            confidence=0.9,
            review_layers=(),
            classic_basis=(),
            reasons=("missing_classic_basis",),
        ),
        _paper(),
        as_of=NOW.date(),
    )
    assert result.dimensions["foundational"] == 40.0
    assert "missing_classic_basis" in result.reasons
    assert result.tier is ScoreTier.LOW
