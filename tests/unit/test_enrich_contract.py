from __future__ import annotations

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus
from papersteward.pipeline.enrich import (
    EnrichmentProvider,
    EnrichmentSignals,
    NeutralEnricher,
)

PAPER = Paper(
    id="p001",
    title="Synthetic Retrieval Study",
    authors=("Fixture Author",),
    venue="Fixture Venue",
    oa_status=OaStatus.OPEN,
)


def test_neutral_enricher_only_reuses_known_paper_fields():
    signals = NeutralEnricher().enrich(PAPER)

    assert signals == EnrichmentSignals(oa_status=OaStatus.OPEN, source="neutral")
    assert signals.teams == signals.experiment_quality == ()
    assert signals.citation_count is signals.citation_velocity is None
    assert signals.code_url is signals.data_url is None
    assert signals.confidence == 0.0


def test_neutral_enricher_satisfies_protocol_and_rejects_wrong_input():
    enricher = NeutralEnricher()

    assert isinstance(enricher, EnrichmentProvider)
    with pytest.raises(TypeError):
        enricher.enrich("not-a-paper")  # type: ignore[arg-type]


def test_signals_accept_typed_evidence_and_normalize_numbers():
    signals = EnrichmentSignals(
        teams=("Team A",),
        venue_level="A*",
        citation_count=12,
        citation_velocity=3,
        code_url="https://example.invalid/code",
        data_url="https://example.invalid/data",
        oa_status=OaStatus.OPEN,
        experiment_quality=("ablation",),
        confidence=1,
        source="fixture",
    )

    assert signals.citation_velocity == 3.0
    assert signals.confidence == 1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"citation_count": -1},
        {"citation_count": True},
        {"citation_velocity": -0.1},
        {"confidence": 1.1},
        {"confidence": True},
        {"code_url": "ftp://example.invalid/code"},
        {"code_url": "https://user:pass@example.invalid/code"},
        {"source": "   "},
        {"teams": ("same", "same")},
        {"teams": "not-a-sequence"},
    ],
)
def test_invalid_signals_are_rejected(kwargs):
    with pytest.raises(ValueError):
        EnrichmentSignals(**kwargs)
