from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Config,
    Event,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    Rating,
    ResearchProfile,
    Run,
    ScoreSnapshot,
    Summary,
    Tag,
    Taxonomy,
)
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    DocumentStatus,
    EventType,
    OaStatus,
    PaperStatus,
    ReadMode,
    RunStatus,
    ScoreTier,
    SpecialBucketKind,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import InvalidTransition, InvariantViolation
from papersteward.storage import (
    ArchiveSnapshotRepository,
    BucketRepository,
    ConfigRepository,
    EventRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ProfileRepository,
    RatingRepository,
    RunRepository,
    ScoreSnapshotRepository,
    SummaryRepository,
    TaxonomyRepository,
)
from papersteward.storage.mappers import score_snapshot_to_row

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
SCORE_DATE = date(2026, 10, 21)
INPUT_HASH = "c" * 64


def _provenance() -> Provenance:
    return Provenance(
        model="fake-llm",
        prompt_version="test/v1",
        generated_at=NOW,
        input_hash=INPUT_HASH,
    )


def _paper(
    paper_id: str = "p001",
    *,
    first_seen_at: datetime = NOW,
    status: PaperStatus = PaperStatus.DISCOVERED,
) -> Paper:
    return Paper(
        id=paper_id,
        title=f"Paper {paper_id}",
        status=status,
        authors=("Alice", "Bob"),
        venue="TestConf",
        published_at=NOW - timedelta(days=3),
        first_seen_at=first_seen_at,
        abstract="An abstract.",
        doi="10.1000/test",
        arxiv_id="2601.00001",
        url="https://example.test/paper",
        pdf_url="https://example.test/paper.pdf",
        oa_status=OaStatus.OPEN,
        canonical_id=paper_id,
    )


def _tag(
    code: str = "T01",
    *,
    role: TagRole = TagRole.PRIMARY,
    confidence: float = 0.9,
    paper_id: str = "p001",
) -> PaperTag:
    return PaperTag(
        paper_id=paper_id,
        tag_code=code,
        role=role,
        taxonomy_version="1.0",
        confidence=confidence,
        assigned_by=AssignedBy.USER,
    )


def _topic_bucket() -> Bucket:
    return Bucket(id="b-t01", name="检索", bucket_type=BucketType.TOPIC, tag_code="T01")


def _review_bucket() -> Bucket:
    return Bucket(
        id="b-review",
        name="跨层综述",
        bucket_type=BucketType.SPECIAL,
        special_kind=SpecialBucketKind.REVIEW,
    )


def _bucket(
    bucket_id: str = "b-t01",
    *,
    role: BucketRole = BucketRole.FILING,
    paper_id: str = "p001",
) -> PaperBucket:
    return PaperBucket(paper_id=paper_id, bucket_id=bucket_id, role=role)


def _summary(*, status: SummaryValidationStatus = SummaryValidationStatus.PASSED) -> Summary:
    return Summary(
        paper_id="p001",
        summary_100="提出层次化检索与跨段重排；提升 2–4 点；延迟较高。",
        what="跨段检索",
        how="层次化检索与重排",
        finding="提升 2–4 点",
        limitation="延迟较高",
        evidence=("原文引文",),
        validation_status=status,
        provenance=_provenance(),
    )


def _score(
    *,
    paper_id: str = "p001",
    score_date: date = SCORE_DATE,
    priority: float | None = 70.0,
    tier: ScoreTier | None = None,
) -> ScoreSnapshot:
    return ScoreSnapshot(
        paper_id=paper_id,
        score_date=score_date,
        relevance=70.0,
        authority=50.0,
        extra_value=50.0,
        static_score=61.5,
        freshness=60.0,
        hunger=50.0,
        dynamic_score=56.0,
        preference=50.0,
        priority=priority if tier is None else None,
        tier=tier,
        breakdown_json={"relevance": {"semantic": 0.8}},
        config_version="1.0.0",
        profile_version="1.0",
        taxonomy_version="1.0",
    )


def _taxonomy(*, status: DocumentStatus = DocumentStatus.CONFIRMED) -> Taxonomy:
    return Taxonomy(
        id="tax-1",
        version="1.0",
        status=status,
        content_md="# Taxonomy",
        tags=(
            Tag(code="T01", name="检索", level=TagLevel.PRIMARY, definition="retrieval"),
            Tag(
                code="S01",
                name="稀疏检索",
                level=TagLevel.SECONDARY,
                parent_code="T01",
                aliases=("sparse",),
            ),
        ),
        confirmed_at=NOW if status is not DocumentStatus.DRAFT else None,
    )


def _profile(
    version: str = "1.0", *, status: DocumentStatus = DocumentStatus.CONFIRMED
) -> ResearchProfile:
    return ResearchProfile(
        id="profile-1",
        version=version,
        status=status,
        content_md="# Profile",
        parsed_json={"core_questions": ["Q1"]},
        confirmed_at=NOW if status is not DocumentStatus.DRAFT else None,
        change_log=("initial",),
    )


def test_paper_round_trip_preserves_domain_fields_and_utc(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    created = repository.add(_paper())

    db_session.commit()
    db_session.expire_all()

    loaded = repository.get("p001")
    assert loaded == created
    assert loaded is not None
    assert loaded.first_seen_at.utcoffset() == timedelta(0)
    assert loaded.created_at == NOW
    assert loaded.authors == ("Alice", "Bob")


def test_paper_list_orders_paginates_and_counts(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.add(_paper("p002", first_seen_at=NOW + timedelta(minutes=2)))
    repository.add(_paper("p001", first_seen_at=NOW))
    repository.add(_paper("p003", first_seen_at=NOW + timedelta(minutes=1)))
    db_session.commit()

    assert repository.count() == 3
    assert [paper.id for paper in repository.list()] == ["p001", "p003", "p002"]
    assert [paper.id for paper in repository.list(limit=2)] == ["p001", "p003"]
    assert [paper.id for paper in repository.list(limit=1, offset=1)] == ["p003"]
    assert repository.get("missing") is None


def test_repository_add_does_not_commit(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.add(_paper())

    db_session.rollback()

    assert repository.get("p001") is None


def test_duplicate_paper_id_raises_integrity_error(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.add(_paper())
    db_session.commit()

    with pytest.raises(IntegrityError):
        repository.add(_paper())
    db_session.rollback()


def test_transition_updates_status_and_appends_event(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    events = EventRepository(db_session)
    papers.add(_paper())

    updated = papers.transition(
        "p001", PaperStatus.DEDUPED, event_id="e1", payload={"source": "test"}
    )

    assert updated is not None
    assert updated.status is PaperStatus.DEDUPED
    event = events.get("e1")
    assert event is not None
    assert event.event_type is EventType.PAPER_DEDUPED
    assert event.paper_id == "p001"
    assert event.payload_json == {"source": "test"}

    with pytest.raises(InvalidTransition):
        papers.transition("p001", PaperStatus.QUEUED, event_id="e2")

    current = papers.get("p001")
    assert current is not None
    assert current.status is PaperStatus.DEDUPED
    assert len(events.list_for_paper("p001")) == 1
    assert papers.transition("missing", PaperStatus.DEDUPED, event_id="e3") is None


def test_event_log_is_queryable_and_append_only(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    events = EventRepository(db_session)
    papers.add(_paper())
    events.append(
        Event(id="e2", event_type=EventType.PAPER_DONE, created_at=NOW + timedelta(seconds=1))
    )
    events.append(Event(id="e1", event_type=EventType.PAPER_PUSHED, created_at=NOW))
    db_session.commit()

    assert [event.id for event in events.recent(limit=10)] == ["e1", "e2"]
    assert events.get("missing") is None
    assert not hasattr(events, "update")
    assert not hasattr(events, "delete")

    with pytest.raises(IntegrityError):
        events.append(Event(id="e1", event_type=EventType.PAPER_DONE))
    db_session.rollback()


def test_paper_tag_primary_is_unique_in_database(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    tags = PaperTagRepository(db_session)
    papers.add(_paper())
    tags.add(_tag("T01"))
    tags.add(_tag("S01", role=TagRole.SECONDARY))
    db_session.commit()

    stored = tags.list_for_paper("p001")
    assert [tag.tag_code for tag in stored] == ["T01", "S01"]
    assert stored[0].role is TagRole.PRIMARY

    with pytest.raises(IntegrityError):
        tags.add(_tag("T02"))
    db_session.rollback()

    assert len([tag for tag in tags.list_for_paper("p001") if tag.role is TagRole.PRIMARY]) == 1


def test_tag_requires_existing_paper(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        PaperTagRepository(db_session).add(_tag(paper_id="missing"))
    db_session.rollback()


def test_paper_bucket_filing_is_unique_and_memberships_allowed(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    buckets = BucketRepository(db_session)
    assignments = PaperBucketRepository(db_session)
    buckets.add(_topic_bucket())
    buckets.add(_review_bucket())
    papers.add(_paper())
    assignments.add(_bucket("b-t01"))
    assignments.add(_bucket("b-review", role=BucketRole.MEMBERSHIP))
    db_session.commit()

    stored = assignments.list_for_paper("p001")
    assert [(item.bucket_id, item.role) for item in stored] == [
        ("b-t01", BucketRole.FILING),
        ("b-review", BucketRole.MEMBERSHIP),
    ]

    with pytest.raises(IntegrityError):
        assignments.add(_bucket("b-review"))
    db_session.rollback()

    assert (
        len([item for item in assignments.list_for_paper("p001") if item.role is BucketRole.FILING])
        == 1
    )


def test_summary_round_trip_and_upsert(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    summaries = SummaryRepository(db_session)
    papers.add(_paper())
    summaries.save(_summary())
    db_session.commit()

    loaded = summaries.get("p001")
    assert loaded == _summary()

    updated = replace(
        _summary(),
        summary_100="更新后的摘要；仍需人工复核；延迟未变。",
        validation_status=SummaryValidationStatus.NEEDS_REVIEW,
    )
    summaries.save(updated)
    db_session.commit()
    db_session.expire_all()

    assert summaries.get("p001") == updated


def test_score_snapshots_append_and_latest(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    snapshots = ScoreSnapshotRepository(db_session)
    papers.add(_paper())

    snapshots.append(_score())
    db_session.commit()
    snapshots.append(_score(score_date=SCORE_DATE + timedelta(days=1), priority=72.0))

    assert len(snapshots.history("p001")) == 2
    latest = snapshots.latest("p001")
    assert latest is not None
    assert latest.priority == 72.0
    assert latest.breakdown_json == {"relevance": {"semantic": 0.8}}

    with pytest.raises(InvariantViolation, match="同一天"):
        snapshots.append(_score())
    db_session.rollback()


def test_rating_and_config_round_trip(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    ratings = RatingRepository(db_session)
    configs = ConfigRepository(db_session)
    papers.add(_paper())
    ratings.add(
        Rating(
            paper_id="p001",
            score=5,
            note="值得复现",
            tags_at_read=("T01", "S01"),
            read_mode=ReadMode.DEEP,
            created_at=NOW,
        )
    )
    configs.add(
        Config(
            key="scoring",
            version="1.0.0",
            value_json={"version": "1.0.0"},
            updated_at=NOW,
        )
    )
    configs.add(
        Config(
            key="scoring",
            version="1.1.0",
            value_json={"version": "1.1.0"},
            updated_at=NOW + timedelta(minutes=1),
        )
    )
    db_session.commit()

    stored_ratings = ratings.list_for_paper("p001")
    assert len(stored_ratings) == 1
    assert stored_ratings[0].read_mode is ReadMode.DEEP
    assert configs.latest("scoring") == configs.get("scoring", "1.1.0")
    assert [config.version for config in configs.versions("scoring")] == ["1.0.0", "1.1.0"]
    assert configs.latest("missing") is None

    with pytest.raises(IntegrityError):
        configs.add(Config(key="scoring", version="1.0.0", value_json={}))
    db_session.rollback()


def test_run_round_trip_and_transition(db_session: Session) -> None:
    runs = RunRepository(db_session)
    run = Run(id="run-1", run_type="ingest", started_at=NOW, stats_json={"found": 3})
    runs.add(run)

    updated = runs.transition("run-1", RunStatus.SUCCEEDED, finished_at=NOW + timedelta(seconds=5))

    assert updated is not None
    assert updated.status is RunStatus.SUCCEEDED
    assert runs.get("run-1") == updated
    assert runs.transition("missing", RunStatus.FAILED) is None

    with pytest.raises(InvalidTransition):
        runs.transition("run-1", RunStatus.RUNNING)


def test_profile_and_taxonomy_version_round_trip(db_session: Session) -> None:
    profiles = ProfileRepository(db_session)
    taxonomies = TaxonomyRepository(db_session)
    profiles.add(_profile("0.9", status=DocumentStatus.DRAFT))
    profiles.add(_profile())
    taxonomies.add(_taxonomy())
    db_session.commit()

    assert profiles.latest_confirmed("profile-1") == _profile()
    assert len(profiles.list("profile-1")) == 2
    assert taxonomies.get("tax-1", "1.0") == _taxonomy()
    assert taxonomies.latest_confirmed("tax-1") == _taxonomy()

    with pytest.raises(IntegrityError):
        profiles.add(_profile("1.1", status=DocumentStatus.CONFIRMED))
    db_session.rollback()


def test_archive_snapshot_is_insert_only(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    archives = ArchiveSnapshotRepository(db_session)
    papers.add(_paper())
    snapshot = ArchiveSnapshot(
        paper_id="p001",
        archived_at=NOW,
        primary_tag="T01",
        filing_bucket_id="b-t01",
        profile_version="1.0",
        taxonomy_version="1.0",
        config_version="1.0.0",
        content_hash=INPUT_HASH,
        secondary_tags=("S01",),
        rating=5,
        note="值得复现",
        score_date=SCORE_DATE,
        priority=70.0,
    )
    archives.add(snapshot)
    db_session.commit()

    assert archives.get("p001") == snapshot
    assert archives.get("missing") is None
    assert not hasattr(archives, "update")
    assert not hasattr(archives, "delete")

    with pytest.raises(IntegrityError):
        archives.add(snapshot)
    db_session.rollback()


def test_bulk_assignment_helpers_and_list_queries(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    tags = PaperTagRepository(db_session)
    buckets = BucketRepository(db_session)
    assignments = PaperBucketRepository(db_session)
    profiles = ProfileRepository(db_session)
    taxonomies = TaxonomyRepository(db_session)
    runs = RunRepository(db_session)

    papers.add(_paper())
    buckets.add(_topic_bucket())
    buckets.add(_review_bucket())
    tags.add_many((_tag("T01"), _tag("S01", role=TagRole.SECONDARY)))
    assignments.add_many((_bucket("b-t01"), _bucket("b-review", role=BucketRole.MEMBERSHIP)))
    profiles.add(_profile())
    taxonomies.add(_taxonomy())
    runs.add(Run(id="run-1", run_type="ingest", started_at=NOW))
    db_session.commit()

    assert {tag.tag_code for tag in tags.list_for_paper("p001")} == {"T01", "S01"}
    assert {item.bucket_id for item in assignments.list_for_paper("p001")} == {
        "b-t01",
        "b-review",
    }
    assert [bucket.id for bucket in buckets.list(active_only=True)] == [
        "b-review",
        "b-t01",
    ]
    assert taxonomies.list() == [_taxonomy()]
    assert taxonomies.latest_confirmed() == _taxonomy()
    assert profiles.list() == [_profile()]
    assert [run.id for run in runs.list()] == ["run-1"]


def test_score_snapshot_primary_key_enforces_daily_uniqueness(db_session: Session) -> None:
    papers = PaperRepository(db_session)
    snapshots = ScoreSnapshotRepository(db_session)
    papers.add(_paper())
    snapshots.append(_score())
    db_session.commit()

    db_session.add(score_snapshot_to_row(_score()))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
