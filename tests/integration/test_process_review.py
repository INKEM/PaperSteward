from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from papersteward.core.entities import Bucket, Paper
from papersteward.core.enums import BucketType, DocumentStatus, OaStatus, PaperStatus, TagRole
from papersteward.documents.profile import parse_profile
from papersteward.documents.taxonomy import parse_taxonomy
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.process import process_paper
from papersteward.pipeline.review import approve_review, list_open_reviews, relabel_review
from papersteward.scoring.config import load_scoring_config_file
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ReviewRepository,
    TaxonomyRepository,
    create_session_factory,
    session_scope,
)
from papersteward.storage.repositories import StaticScoreSnapshotRepository

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
PROFILE = replace(
    parse_profile(
        (ROOT / "tests/fixtures/profiles/basic.md").read_text(encoding="utf-8"),
        source="basic.md",
        document_id="demo-profile",
    ),
    status=DocumentStatus.CONFIRMED,
    confirmed_at=NOW,
)
TAXONOMY = replace(
    parse_taxonomy(
        (ROOT / "tests/fixtures/taxonomies/basic.md").read_text(encoding="utf-8"),
        source="basic.md",
        document_id="demo-taxonomy",
    ),
    status=DocumentStatus.CONFIRMED,
    confirmed_at=NOW,
)
CONFIG = load_scoring_config_file(ROOT / "tests/fixtures/configs/scoring.yaml")
PAPER = Paper(
    id="proc-001",
    title="Retrieval-augmented generation for long-document QA",
    abstract=(
        "We study hierarchical retrieval for long-document QA with sparse retrieval. "
        "Paragraph clustering recall and cross-layer attention reranking improve grounding. "
        "This improves EM by 2–4 points, but offline clustering adds latency."
    ),
    oa_status=OaStatus.OPEN,
    status=PaperStatus.SCREENED,
    first_seen_at=NOW,
)


def _buckets() -> dict[str, Bucket]:
    return {
        f"T{index:02d}": Bucket(
            id=f"b-t{index:02d}",
            name=f"Topic T{index:02d}",
            bucket_type=BucketType.TOPIC,
            tag_code=f"T{index:02d}",
        )
        for index in range(1, 6)
    }


def _gateway() -> LLMGateway:
    data = (ROOT / "tests/fixtures/process/valid.json").read_text(encoding="utf-8")
    import json

    payload = json.loads(data)
    responses = [
        json.dumps(payload["relevance"], ensure_ascii=False),
        json.dumps(payload["summary"], ensure_ascii=False),
        json.dumps(payload["classification"], ensure_ascii=False),
        json.dumps(payload["special_nomination"], ensure_ascii=False),
    ]
    return LLMGateway(FakeLLM(responses, model_id="fake-llm"), clock=lambda: NOW)


def _setup(session):
    PaperRepository(session).add(PAPER)
    TaxonomyRepository(session).add(TAXONOMY)
    for bucket in _buckets().values():
        BucketRepository(session).add(bucket)


def _process(session):
    return process_paper(
        session,
        PAPER,
        profile=PROFILE,
        taxonomy=TAXONOMY,
        topic_buckets=_buckets(),
        config=CONFIG,
        gateway=_gateway(),
        embedding_provider=HashingEmbeddingProvider(),
        now=NOW,
        score_date=NOW.date(),
    )


def test_process_opens_review_and_approve_closes_it(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        outcome = _process(session)
        assert outcome.needs_review is True
        assert "classification:low_primary_confidence" in outcome.reasons
        assert outcome.status is PaperStatus.NEEDS_REVIEW
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.NEEDS_REVIEW
        assert StaticScoreSnapshotRepository(session).latest(PAPER.id) is not None
        items, total = list_open_reviews(session)
        assert total == 1 and items[0][0].paper_id == PAPER.id

        approved = approve_review(session, PAPER.id, reviewed_by="fixture-user", now=NOW)
        assert approved.status.value == "resolved"
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.CLASSIFIED
        assert list_open_reviews(session)[1] == 0


def test_process_then_relabel_replaces_tags_and_filing(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        result = relabel_review(
            session,
            PAPER.id,
            primary_tag="T03",
            secondary_tags=("S01",),
            reviewed_by="fixture-user",
            now=NOW,
        )
        assert result.resolution.value == "relabeled"
        tags = PaperTagRepository(session).list_for_paper(PAPER.id)
        assert [(tag.tag_code, tag.role) for tag in tags] == [
            ("T03", TagRole.PRIMARY),
            ("S01", TagRole.SECONDARY),
        ]
        buckets = PaperBucketRepository(session).list_for_paper(PAPER.id)
        assert [bucket.bucket_id for bucket in buckets] == ["b-t03"]
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.CLASSIFIED
        assert ReviewRepository(session).get(PAPER.id).resolution.value == "relabeled"  # type: ignore[union-attr]
