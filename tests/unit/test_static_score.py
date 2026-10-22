from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.static import (
    AuthorityInputs,
    ExtraValueInputs,
    StaticScoreError,
    build_static_snapshot,
    compute_authority,
    compute_extra_value,
    compute_static_score,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "scoring" / "static_golden.json").read_text())
CONFIG = load_scoring_config_file(ROOT / "tests" / "fixtures" / "configs" / "scoring.yaml")
NOW = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
PAPER = Paper(id="static-001", title="Synthetic static score paper", first_seen_at=NOW)


def test_authority_and_extra_value_match_golden():
    authority_data = GOLDEN["authority"]
    authority = compute_authority(
        AuthorityInputs(
            venue_level=authority_data["venue_level"],
            author_scores=tuple(authority_data["author_scores"]),
            citation_percentile=authority_data["citation_percentile"],
            published_at=datetime.fromisoformat(
                authority_data["published_at"].replace("Z", "+00:00")
            ),
            as_of=date.fromisoformat(authority_data["as_of"]),
        )
    )
    assert authority.score == authority_data["expected"]
    assert authority.confidence == 1.0 and authority.reasons == ()

    extra_data = GOLDEN["extra_value"]
    extra = compute_extra_value(
        ExtraValueInputs(
            code=extra_data["code"],
            data=extra_data["data"],
            reproducibility=extra_data["reproducibility"],
            rigor=extra_data["rigor"],
            oa_status=OaStatus(extra_data["oa_status"]),
        )
    )
    assert extra.score == extra_data["expected"]
    assert extra.confidence == 1.0 and extra.reasons == ()

    static_data = GOLDEN["static"]
    result = compute_static_score(static_data["relevance"], authority, extra, CONFIG)
    assert result.static_score == static_data["expected"]
    assert result.confidence == 1.0
    assert result.breakdown["weights"] == {
        "relevance": 0.65,
        "authority": 0.2,
        "extra_value": 0.15,
    }


def test_unknown_signals_use_neutral_values_and_lower_confidence():
    authority = compute_authority(AuthorityInputs(as_of=date(2026, 10, 22)))
    assert authority.score == 50.0
    assert authority.reasons == (
        "unknown_venue",
        "missing_author",
        "missing_citation",
    )
    assert authority.confidence == pytest.approx(0.4)

    extra = compute_extra_value(ExtraValueInputs())
    assert extra.score == 50.0
    assert extra.confidence == 0.0
    assert set(extra.reasons) == {
        "missing_code",
        "missing_data",
        "missing_reproducibility",
        "missing_rigor",
        "unknown_open",
    }


def test_young_paper_citation_is_neutral_and_predatory_venue_is_zero():
    young = compute_authority(
        AuthorityInputs(
            venue_level="A",
            author_scores=(0.8,),
            citation_percentile=0.9,
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
            as_of=date(2026, 10, 22),
        )
    )
    assert young.citation == 0.5
    assert "young_citation" in young.reasons

    predatory = compute_authority(
        AuthorityInputs(
            venue_level="predatory",
            author_scores=(0.8,),
            citation_percentile=0.9,
            published_at=datetime(2020, 1, 1, tzinfo=UTC),
            as_of=date(2026, 10, 22),
        )
    )
    assert predatory.venue == 0.0
    assert predatory.score == pytest.approx(50.0)


def test_extra_value_open_access_mapping():
    closed = compute_extra_value(
        ExtraValueInputs(code=1, data=1, reproducibility=1, rigor=1, oa_status=OaStatus.CLOSED)
    )
    assert closed.open == 0.0 and closed.score == 90.0
    unknown = compute_extra_value(
        ExtraValueInputs(code=1, data=1, reproducibility=1, rigor=1, oa_status=OaStatus.UNKNOWN)
    )
    assert unknown.open == 0.5 and unknown.score == 95.0


def test_build_snapshot_is_deterministic_and_versioned():
    authority = compute_authority(
        AuthorityInputs(
            venue_level="A",
            author_scores=(0.8,),
            citation_percentile=0.7,
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
            as_of=date(2026, 10, 22),
        )
    )
    extra = compute_extra_value(ExtraValueInputs(code=1, data=0.5, oa_status=OaStatus.OPEN))
    result = compute_static_score(75.0, authority, extra, CONFIG)
    first = build_static_snapshot(
        result,
        PAPER,
        score_date=date(2026, 10, 22),
        config_version=CONFIG.version,
        profile_version="1.0",
        taxonomy_version="1.0",
        created_at=NOW,
    )
    second = build_static_snapshot(
        result,
        PAPER,
        score_date=date(2026, 10, 22),
        config_version=CONFIG.version,
        profile_version="1.0",
        taxonomy_version="1.0",
        created_at=NOW,
    )
    assert first.input_hash == second.input_hash
    assert len(first.input_hash) == 64
    assert first.breakdown_json["static_score"] == result.static_score


@pytest.mark.parametrize(
    "kwargs",
    [
        {"author_scores": (1.2,)},
        {"citation_percentile": -0.1},
        {"as_of": datetime(2026, 10, 22, tzinfo=UTC)},
    ],
)
def test_invalid_authority_inputs(kwargs):
    with pytest.raises(StaticScoreError):
        AuthorityInputs(**kwargs)


def test_invalid_extra_value_and_static_range():
    with pytest.raises(StaticScoreError):
        ExtraValueInputs(code=1.2)
    authority = compute_authority(AuthorityInputs(as_of=date(2026, 10, 22)))
    extra = compute_extra_value(ExtraValueInputs())
    with pytest.raises(StaticScoreError):
        compute_static_score(101.0, authority, extra, CONFIG)
