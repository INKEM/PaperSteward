from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from papersteward.core.entities import (
    Enrichment,
    EnrichmentSignals,
    Paper,
    Provenance,
)
from papersteward.core.enums import OaStatus
from papersteward.pipeline.enrich_service import persist_enrichment
from papersteward.storage import (
    EnrichmentRepository,
    PaperRepository,
    create_session_factory,
    session_scope,
)
from papersteward.storage.models import EnrichmentRow

T0 = datetime(2026, 10, 22, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _paper(paper_id: str = "p001") -> Paper:
    return Paper(
        id=paper_id,
        title="Synthetic Enrichment Study",
        authors=("Fixture Author",),
        oa_status=OaStatus.OPEN,
        first_seen_at=T0,
    )


def test_neutral_enrichment_persists_and_repeats_idempotently(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        paper = _paper()
        PaperRepository(session).add(paper)
        first, changed = persist_enrichment(session, paper, now=lambda: T0)
        assert changed is True
        assert first.signals == EnrichmentSignals(oa_status=OaStatus.OPEN, source="neutral")
        assert first.provenance is None
        assert first.created_at == T0 and first.updated_at == T0

        second, changed = persist_enrichment(session, paper, now=lambda: T1)
        assert changed is False
        assert second.updated_at == T0
        assert session.scalar(select(func.count()).select_from(EnrichmentRow)) == 1


def test_repository_updates_changed_signals_and_preserves_created_at(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        paper = _paper()
        PaperRepository(session).add(paper)
        persist_enrichment(session, paper, now=lambda: T0)
        repository = EnrichmentRepository(session)
        signals = EnrichmentSignals(
            teams=("Team A",),
            venue_level="A*",
            citation_count=10,
            citation_velocity=2.5,
            code_url="https://example.invalid/code",
            data_url="https://example.invalid/data",
            oa_status=OaStatus.OPEN,
            experiment_quality=("ablation",),
            confidence=0.8,
            source="fixture",
        )
        provenance = Provenance(
            model="fixture-model",
            prompt_version="enrichment/v1",
            generated_at=T1,
            input_hash="a" * 64,
        )
        updated, changed = repository.save(
            Enrichment(
                paper_id=paper.id,
                signals=signals,
                created_at=T1,
                updated_at=T1,
                provenance=provenance,
            )
        )
        assert changed is True
        assert updated.created_at == T0 and updated.updated_at == T1
        assert updated.signals == signals and updated.provenance == provenance

        reloaded = repository.get(paper.id)
        assert reloaded is not None
        assert reloaded.signals == signals
        assert reloaded.provenance == provenance

        repeated, changed = repository.save(
            Enrichment(
                paper_id=paper.id,
                signals=signals,
                created_at=T1,
                updated_at=T2,
                provenance=provenance,
            )
        )
        assert changed is False
        assert repeated.updated_at == T1


def test_repository_lists_enrichment_in_stable_order(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        for paper_id in ("p002", "p001"):
            paper = _paper(paper_id)
            PaperRepository(session).add(paper)
            persist_enrichment(session, paper, now=lambda: T0)

        assert [item.paper_id for item in EnrichmentRepository(session).list()] == [
            "p001",
            "p002",
        ]


def test_missing_paper_is_rejected(migrated_engine):
    factory = create_session_factory(migrated_engine)
    enrichment = Enrichment(
        paper_id="missing",
        signals=EnrichmentSignals(),
        created_at=T0,
        updated_at=T0,
    )
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        EnrichmentRepository(session).save(enrichment)
