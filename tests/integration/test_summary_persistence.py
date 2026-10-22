from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus, SummaryValidationStatus
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.summarize import generate_summary, persist_summary
from papersteward.storage import (
    PaperRepository,
    SummaryRepository,
    create_session_factory,
    session_scope,
)
from papersteward.storage.models import SummaryRow

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "summaries"
NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
PAPER = Paper(
    id="sum-001",
    title="Hierarchical retrieval and reranking for long-document QA",
    abstract=(
        "We propose hierarchical retrieval for long-document QA. "
        "Paragraph clustering recall and cross-layer attention reranking "
        "form our retrieval pipeline. "
        "The method improves EM by 2–4 points on three long-document QA benchmarks, "
        "but offline clustering adds latency."
    ),
    oa_status=OaStatus.OPEN,
    first_seen_at=NOW,
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _gateway(response: str) -> LLMGateway:
    return LLMGateway(FakeLLM([response], model_id="fake-llm"), clock=lambda: NOW)


def test_passed_summary_round_trips_with_provenance(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        result = generate_summary(PAPER, _gateway(_fixture("valid.json")))
        persist_summary(session, result)

    with session_scope(factory) as session:
        stored = SummaryRepository(session).get(PAPER.id)
        assert stored is not None
        assert stored.validation_status is SummaryValidationStatus.PASSED
        assert stored.summary_100 == result.summary.summary_100
        assert stored.evidence == result.summary.evidence
        assert stored.provenance is not None
        assert stored.provenance.prompt_version == "summary/v2"


def test_needs_review_summary_is_persisted_not_dropped(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        result = generate_summary(PAPER, _gateway(_fixture("missing_evidence.json")))
        persist_summary(session, result)
        assert result.needs_review is True

    with session_scope(factory) as session:
        stored = SummaryRepository(session).get(PAPER.id)
        assert stored is not None
        assert stored.validation_status is SummaryValidationStatus.NEEDS_REVIEW
        assert stored.evidence == ("improves EM by 2–4 points",)


def test_regeneration_overwrites_single_summary_row(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        persist_summary(session, generate_summary(PAPER, _gateway(_fixture("valid.json"))))
    with session_scope(factory) as session:
        persist_summary(session, generate_summary(PAPER, _gateway(_fixture("vague.json"))))
        assert session.scalar(select(func.count()).select_from(SummaryRow)) == 1
    with session_scope(factory) as session:
        stored = SummaryRepository(session).get(PAPER.id)
        assert stored is not None
        assert stored.validation_status is SummaryValidationStatus.NEEDS_REVIEW
