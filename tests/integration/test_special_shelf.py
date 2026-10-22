from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.entities import (
    Bucket,
    NominationEvidence,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    SpecialNominationRecord,
)
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    DocumentStatus,
    EventType,
    PaperStatus,
    SpecialBucketKind,
    SpecialNominationStatus,
    TagRole,
)
from papersteward.documents.profile import parse_profile
from papersteward.documents.taxonomy import parse_taxonomy
from papersteward.errors import ConflictError
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.process import process_paper
from papersteward.pipeline.review import list_open_reviews
from papersteward.pipeline.shelf import (
    accept_nomination,
    ensure_special_buckets,
    list_pending_nominations,
    persist_nominations,
    reject_nomination,
)
from papersteward.scoring.config import load_scoring_config_file
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    SpecialNominationRepository,
    TaxonomyRepository,
    create_session_factory,
    session_scope,
)
from papersteward.storage.repositories import EventRepository, ReviewRepository

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
PROFILE = parse_profile(
    (ROOT / "tests/fixtures/profiles/basic.md").read_text(encoding="utf-8"),
    source="basic.md",
    document_id="demo-profile",
)
PROFILE = PROFILE.__class__(
    **{
        **{field: getattr(PROFILE, field) for field in PROFILE.__dataclass_fields__},
        "status": DocumentStatus.CONFIRMED,
        "confirmed_at": NOW,
    }
)
TAXONOMY = parse_taxonomy(
    (ROOT / "tests/fixtures/taxonomies/basic.md").read_text(encoding="utf-8"),
    source="basic.md",
    document_id="demo-taxonomy",
)
TAXONOMY = TAXONOMY.__class__(
    **{
        **{field: getattr(TAXONOMY, field) for field in TAXONOMY.__dataclass_fields__},
        "status": DocumentStatus.CONFIRMED,
        "confirmed_at": NOW,
    }
)
CONFIG = load_scoring_config_file(ROOT / "tests/fixtures/configs/scoring.yaml")
PAPER = Paper(
    id="shelf-001",
    title="Retrieval-augmented generation for long-document QA",
    abstract=(
        "We study hierarchical retrieval for long-document QA with sparse retrieval. "
        "Paragraph clustering recall and cross-layer attention reranking improve grounding. "
        "This improves EM by 2–4 points, but offline clustering adds latency."
    ),
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
    payload = json.loads((ROOT / "tests/fixtures/process/special.json").read_text(encoding="utf-8"))
    responses = [
        json.dumps(payload["relevance"], ensure_ascii=False),
        json.dumps(payload["summary"], ensure_ascii=False),
        json.dumps(payload["classification"], ensure_ascii=False),
        json.dumps(payload["special_nomination"], ensure_ascii=False),
    ]
    return LLMGateway(FakeLLM(responses, model_id="fake-llm"), clock=lambda: NOW)


def _setup(session: Session) -> None:
    PaperRepository(session).add(PAPER)
    TaxonomyRepository(session).add(TAXONOMY)
    for bucket in _buckets().values():
        BucketRepository(session).add(bucket)


def _process(session: Session):
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


def _nomination(paper_id: str, reasons: tuple[str, ...] = ()) -> SpecialNominationRecord:
    return SpecialNominationRecord(
        id=f"nom-{paper_id}",
        paper_id=paper_id,
        kind=SpecialBucketKind.REVIEW,
        status=SpecialNominationStatus.PENDING,
        confidence=0.8,
        reason="合成提名",
        evidence=(NominationEvidence("Paragraph clustering recall", True),),
        review_layers=("method", "evaluation"),
        classic_basis=(),
        bridge_memo=None,
        reasons=reasons,
        provenance=Provenance("fixture", "special_nomination/v1", NOW, "0" * 64),
        nominated_at=NOW,
    )


def test_process_persists_nominations_without_paper_review(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        outcome = _process(session)
        assert outcome.needs_review is False
        assert outcome.status is PaperStatus.CLASSIFIED
        assert list_open_reviews(session)[1] == 0

        records = SpecialNominationRepository(session).list_for_paper(PAPER.id)
        assert {record.kind for record in records} == {
            SpecialBucketKind.REVIEW,
            SpecialBucketKind.DISTANT,
        }
        assert all(record.status is SpecialNominationStatus.PENDING for record in records)
        review = next(record for record in records if record.kind is SpecialBucketKind.REVIEW)
        assert review.evidence[0].valid is True
        assert review.review_layers == ("method", "evaluation")
        distant = next(record for record in records if record.kind is SpecialBucketKind.DISTANT)
        assert distant.bridge_memo is not None
        assert "shared_structure" in distant.bridge_memo

        first_ids = {record.kind: record.id for record in records}
        persisted = persist_nominations(session, outcome.special, now=NOW)
        assert {record.kind: record.id for record in persisted} == first_ids
        assert len(SpecialNominationRepository(session).list_for_paper(PAPER.id)) == 2


def test_accept_files_special_bucket_and_rejects_superseded(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        decision = accept_nomination(
            session, PAPER.id, reviewed_by="fixture-user", note="合成审核", now=NOW
        )

        assert decision.nomination.kind is SpecialBucketKind.REVIEW
        assert decision.nomination.status is SpecialNominationStatus.ACCEPTED
        assert decision.bucket is not None and decision.bucket.id == "b-special-review"
        assert decision.paper.status is PaperStatus.SHELVED
        assert [item.kind for item in decision.superseded] == [SpecialBucketKind.DISTANT]

        filings = [
            item
            for item in PaperBucketRepository(session).list_for_paper(PAPER.id)
            if item.role is BucketRole.FILING
        ]
        assert [item.bucket_id for item in filings] == ["b-special-review"]
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.SHELVED
        assert TaxonomyRepository(session).get(TAXONOMY.id, TAXONOMY.version) is not None
        assert [
            tag.tag_code
            for tag in PaperTagRepository(session).list_for_paper(PAPER.id)
            if tag.role is TagRole.PRIMARY
        ] == ["T02"]

        events = EventRepository(session).list_for_paper(PAPER.id)
        by_type = {event.event_type: event for event in events}
        assert by_type[EventType.PAPER_SHELVED].payload_json["special_kind"] == "review"
        assert by_type[EventType.PAPER_BUCKET_MOVED].payload_json["to_bucket_id"] == (
            "b-special-review"
        )

        buckets = ensure_special_buckets(session)
        assert {kind: bucket.id for kind, bucket in buckets.items()} == {
            SpecialBucketKind.REVIEW: "b-special-review",
            SpecialBucketKind.DISTANT: "b-special-distant",
            SpecialBucketKind.CLASSIC: "b-special-classic",
        }
        with pytest.raises(ConflictError, match="没有待审"):
            accept_nomination(session, PAPER.id, reviewed_by="fixture-user", now=NOW)


def test_reject_keeps_topic_filing_and_status(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        decision = reject_nomination(
            session,
            PAPER.id,
            kind=SpecialBucketKind.REVIEW,
            reviewed_by="fixture-user",
            note="主线更合适",
            now=NOW,
        )
        assert decision.nomination.status is SpecialNominationStatus.REJECTED
        assert decision.bucket is None
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.CLASSIFIED
        filing = [
            item
            for item in PaperBucketRepository(session).list_for_paper(PAPER.id)
            if item.role is BucketRole.FILING
        ][0]
        assert filing.bucket_id == "b-t02"

        pending, total = list_pending_nominations(session)
        assert total == 1 and pending[0][0].kind is SpecialBucketKind.DISTANT
        reject_nomination(session, PAPER.id, reviewed_by="fixture-user", now=NOW)
        assert list_pending_nominations(session)[1] == 0


def test_accept_requires_classified_and_force_with_note(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        PaperRepository(session).add(
            Paper(
                id="shelf-review",
                title="Needs review paper",
                status=PaperStatus.NEEDS_REVIEW,
                first_seen_at=NOW,
            )
        )
        SpecialNominationRepository(session).upsert(
            _nomination("shelf-review", ("low_confidence",))
        )
        with pytest.raises(ConflictError, match="classified"):
            accept_nomination(
                session, "shelf-review", reviewed_by="fixture-user", force=True, note="x", now=NOW
            )

        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.SCREENED
        PaperRepository(session).add(
            Paper(
                id="shelf-forced",
                title="Gate failed paper",
                status=PaperStatus.CLASSIFIED,
                first_seen_at=NOW,
            )
        )
        PaperTagRepository(session).add(PaperTag("shelf-forced", "T02", TagRole.PRIMARY, "1.0"))
        PaperBucketRepository(session).add(PaperBucket("shelf-forced", "b-t02", BucketRole.FILING))
        SpecialNominationRepository(session).upsert(
            _nomination("shelf-forced", ("low_confidence",))
        )
        with pytest.raises(ConflictError, match="未通过校验"):
            accept_nomination(session, "shelf-forced", reviewed_by="fixture-user", now=NOW)
        with pytest.raises(ConflictError, match="备注"):
            accept_nomination(
                session, "shelf-forced", reviewed_by="fixture-user", force=True, now=NOW
            )
        decision = accept_nomination(
            session,
            "shelf-forced",
            reviewed_by="fixture-user",
            force=True,
            note="人工确认证据不足但值得读",
            now=NOW,
        )
        assert decision.nomination.status is SpecialNominationStatus.ACCEPTED
        assert decision.nomination.decision_note == "人工确认证据不足但值得读"

        PaperRepository(session).add(
            Paper(
                id="shelf-open-review",
                title="Open paper review",
                status=PaperStatus.CLASSIFIED,
                first_seen_at=NOW,
            )
        )
        PaperTagRepository(session).add(
            PaperTag("shelf-open-review", "T02", TagRole.PRIMARY, "1.0")
        )
        PaperBucketRepository(session).add(
            PaperBucket("shelf-open-review", "b-t02", BucketRole.FILING)
        )
        ReviewRepository(session).open("shelf-open-review", ["boundary"], {}, now=NOW)
        SpecialNominationRepository(session).upsert(_nomination("shelf-open-review"))
        with pytest.raises(ConflictError, match="普通审核项"):
            accept_nomination(session, "shelf-open-review", reviewed_by="fixture-user", now=NOW)


def test_shelf_cli_lists_and_rejects(migrated_engine, monkeypatch, capsys):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()

    assert main(["shelf", "review", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["total"] == 2
    assert {item["kind"] for item in listing["items"]} == {"review", "distant"}

    assert main(["shelf", "reject", PAPER.id, "--kind", "distant", "--by", "fixture-user"]) == 0
    assert "rejected" in capsys.readouterr().out
    assert main(["shelf", "review", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    get_settings.cache_clear()
