from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Event,
    PaperBucket,
    PaperTag,
    Provenance,
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
    PaperStatus,
    RecalculationTrigger,
    ScoreTier,
    SpecialBucketKind,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import InvariantViolation
from papersteward.core.invariants import (
    choose_special_filing_kind,
    classification_requires_review,
    ensure_archive_snapshot_immutable,
    ensure_bucket_catalog,
    ensure_bucket_references,
    ensure_event_appended,
    ensure_event_history_append_only,
    ensure_low_confidence_routed_to_review,
    ensure_paper_classification,
    ensure_push_eligible,
    ensure_queue_eligible,
    ensure_score_matches_bucket,
    ensure_score_snapshot_append_only,
    ensure_secondary_tag_limit,
    ensure_shelvable,
    ensure_single_filing_bucket,
    ensure_single_primary_tag,
    ensure_static_score_frozen,
    ensure_tags_match_taxonomy,
    ensure_taxonomy_shape,
)

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
SCORE_DATE = date(2026, 10, 21)
INPUT_HASH = "b" * 64


def _provenance() -> Provenance:
    return Provenance(
        model="fake-llm",
        prompt_version="test/v1",
        generated_at=NOW,
        input_hash=INPUT_HASH,
    )


def _tag(
    code: str,
    *,
    level: TagLevel = TagLevel.PRIMARY,
    name: str | None = None,
    active: bool = True,
) -> Tag:
    return Tag(
        code=code,
        name=name or code,
        level=level,
        definition=f"{code} definition",
        active=active,
    )


def _taxonomy(
    *,
    tags: tuple[Tag, ...] | None = None,
    status: DocumentStatus = DocumentStatus.CONFIRMED,
    version: str = "1.0",
) -> Taxonomy:
    return Taxonomy(
        id="tax-1",
        version=version,
        status=status,
        content_md="# taxonomy",
        tags=tags if tags is not None else (_tag("T01"), _tag("S01", level=TagLevel.SECONDARY)),
        confirmed_at=NOW if status is not DocumentStatus.DRAFT else None,
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


def _tag_assignment(
    code: str = "T01",
    *,
    role: TagRole = TagRole.PRIMARY,
    confidence: float = 0.9,
    taxonomy_version: str = "1.0",
    paper_id: str = "p001",
) -> PaperTag:
    return PaperTag(
        paper_id=paper_id,
        tag_code=code,
        role=role,
        taxonomy_version=taxonomy_version,
        confidence=confidence,
        assigned_by=AssignedBy.USER,
    )


def _bucket_assignment(
    bucket_id: str = "b-t01",
    *,
    role: BucketRole = BucketRole.FILING,
    paper_id: str = "p001",
) -> PaperBucket:
    return PaperBucket(paper_id=paper_id, bucket_id=bucket_id, role=role)


def _summary(
    *,
    paper_id: str = "p001",
    status: SummaryValidationStatus = SummaryValidationStatus.PASSED,
) -> Summary:
    return Summary(
        paper_id=paper_id,
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
    relevance: float = 70.0,
    priority: float | None = 70.0,
    tier: ScoreTier | None = None,
    score_date: date = SCORE_DATE,
    static_score: float = 61.5,
    profile_version: str = "1.0",
    taxonomy_version: str = "1.0",
    config_version: str = "1.0.0",
) -> ScoreSnapshot:
    return ScoreSnapshot(
        paper_id=paper_id,
        score_date=score_date,
        relevance=relevance,
        authority=50.0,
        extra_value=50.0,
        static_score=static_score,
        freshness=60.0,
        hunger=50.0,
        dynamic_score=56.0,
        preference=50.0,
        config_version=config_version,
        profile_version=profile_version,
        taxonomy_version=taxonomy_version,
        priority=priority if tier is None else None,
        tier=tier,
    )


def test_exactly_one_primary_tag() -> None:
    with pytest.raises(InvariantViolation, match="恰好有一个主标签"):
        ensure_single_primary_tag([])

    with pytest.raises(InvariantViolation, match="实际 2 个"):
        ensure_single_primary_tag([_tag_assignment("T01"), _tag_assignment("T02")])

    primary = ensure_single_primary_tag([_tag_assignment("T01")])
    assert primary.tag_code == "T01"


def test_primary_tag_must_belong_to_active_taxonomy() -> None:
    tags = [_tag_assignment("T01")]
    assert ensure_tags_match_taxonomy(tags, _taxonomy()).tag_code == "T01"

    with pytest.raises(InvariantViolation, match="引用 2.0"):
        ensure_tags_match_taxonomy([_tag_assignment("T01", taxonomy_version="2.0")], _taxonomy())

    with pytest.raises(InvariantViolation, match="不在分类法"):
        ensure_tags_match_taxonomy([_tag_assignment("T09")], _taxonomy())

    with pytest.raises(InvariantViolation, match="已确认的分类法"):
        ensure_tags_match_taxonomy(tags, _taxonomy(status=DocumentStatus.DRAFT))

    inactive = _taxonomy(tags=(_tag("T01", active=False),))
    with pytest.raises(InvariantViolation, match="已停用"):
        ensure_tags_match_taxonomy(tags, inactive)


def test_secondary_tags_are_capped_and_unique() -> None:
    secondary = [_tag_assignment(f"S{i:02d}", role=TagRole.SECONDARY) for i in range(6)]
    with pytest.raises(InvariantViolation, match="最多 5 个"):
        ensure_secondary_tag_limit(secondary)

    with pytest.raises(InvariantViolation, match="不能重复"):
        ensure_secondary_tag_limit(
            [
                _tag_assignment("S01", role=TagRole.SECONDARY),
                _tag_assignment("S01", role=TagRole.SECONDARY),
            ]
        )

    assert len(ensure_secondary_tag_limit(secondary[:5])) == 5


def test_exactly_one_filing_bucket_and_valid_references() -> None:
    topic = _topic_bucket()
    review = _review_bucket()
    catalog = {topic.id: topic, review.id: review}

    with pytest.raises(InvariantViolation, match="恰好有一个归属桶"):
        ensure_single_filing_bucket([])

    with pytest.raises(InvariantViolation, match="实际 2 个"):
        ensure_single_filing_bucket(
            [
                _bucket_assignment("b-t01"),
                _bucket_assignment("b-review"),
            ]
        )

    filing = ensure_bucket_references(
        [
            _bucket_assignment("b-t01"),
            _bucket_assignment("b-review", role=BucketRole.MEMBERSHIP),
        ],
        catalog,
    )
    assert filing.bucket_id == "b-t01"

    with pytest.raises(InvariantViolation, match="不存在"):
        ensure_bucket_references([_bucket_assignment("b-missing")], catalog)

    with pytest.raises(InvariantViolation, match="重复"):
        ensure_bucket_references(
            [
                _bucket_assignment("b-t01"),
                _bucket_assignment("b-review", role=BucketRole.MEMBERSHIP),
                _bucket_assignment("b-review", role=BucketRole.MEMBERSHIP),
            ],
            catalog,
        )


def test_paper_classification_composite() -> None:
    primary, filing = ensure_paper_classification(
        tags=[
            _tag_assignment("T01"),
            _tag_assignment("S01", role=TagRole.SECONDARY, confidence=0.8),
        ],
        buckets=[
            _bucket_assignment("b-t01"),
            _bucket_assignment("b-review", role=BucketRole.MEMBERSHIP),
        ],
        taxonomy=_taxonomy(),
        buckets_by_id={"b-t01": _topic_bucket(), "b-review": _review_bucket()},
        paper_id="p001",
    )

    assert primary.tag_code == "T01"
    assert filing.bucket_id == "b-t01"


def test_special_bucket_priority_and_catalog() -> None:
    assert (
        choose_special_filing_kind(
            [
                SpecialBucketKind.DISTANT,
                SpecialBucketKind.REVIEW,
                SpecialBucketKind.CLASSIC,
            ]
        )
        is SpecialBucketKind.REVIEW
    )
    assert choose_special_filing_kind([]) is None

    review = _review_bucket()
    classic = Bucket(
        id="b-classic",
        name="经典/奠基",
        bucket_type=BucketType.SPECIAL,
        special_kind=SpecialBucketKind.CLASSIC,
    )
    distant = Bucket(
        id="b-distant",
        name="远邻领域",
        bucket_type=BucketType.SPECIAL,
        special_kind=SpecialBucketKind.DISTANT,
    )
    ensure_bucket_catalog([review, classic, distant], require_all_special=True)

    with pytest.raises(InvariantViolation, match="必须齐全"):
        ensure_bucket_catalog([review, classic], require_all_special=True)

    duplicate_kind = Bucket(
        id="b-review-2",
        name="跨层综述 2",
        bucket_type=BucketType.SPECIAL,
        special_kind=SpecialBucketKind.REVIEW,
    )
    with pytest.raises(InvariantViolation, match="特殊桶类型不能重复"):
        ensure_bucket_catalog([review, duplicate_kind])


def _taxonomy_with_primaries(count: int, *, first_name: str = "主题") -> Taxonomy:
    tags = tuple(
        _tag(f"T{index:02d}", name=first_name if index == 1 else f"主题 {index}")
        for index in range(1, count + 1)
    )
    return _taxonomy(tags=tags)


def test_taxonomy_shape_requires_five_to_nine_primary_tags() -> None:
    with pytest.raises(InvariantViolation, match="5–9"):
        ensure_taxonomy_shape(_taxonomy_with_primaries(4))

    ensure_taxonomy_shape(_taxonomy_with_primaries(5))
    ensure_taxonomy_shape(_taxonomy_with_primaries(9))

    with pytest.raises(InvariantViolation, match="5–9"):
        ensure_taxonomy_shape(_taxonomy_with_primaries(10))

    with pytest.raises(InvariantViolation, match="兜底主标签"):
        ensure_taxonomy_shape(_taxonomy_with_primaries(5, first_name="其他"))

    one_inactive = _taxonomy(tags=tuple(_tag(f"T{i:02d}", active=i != 5) for i in range(1, 6)))
    with pytest.raises(InvariantViolation, match="5–9"):
        ensure_taxonomy_shape(one_inactive)


def test_score_must_match_bucket_type() -> None:
    ensure_score_matches_bucket(_score(priority=70), _topic_bucket())

    with pytest.raises(InvariantViolation, match="必须计算 P"):
        ensure_score_matches_bucket(_score(priority=None, tier=ScoreTier.HIGH), _topic_bucket())

    ensure_score_matches_bucket(_score(priority=None, tier=ScoreTier.HIGH), _review_bucket())

    with pytest.raises(InvariantViolation, match="不得计算 P"):
        ensure_score_matches_bucket(_score(priority=70), _review_bucket())


def test_queue_requires_scored_topic_above_threshold() -> None:
    ensure_queue_eligible(
        status=PaperStatus.SCORED,
        snapshot=_score(relevance=70),
        filing_bucket=_topic_bucket(),
    )

    with pytest.raises(InvariantViolation, match="只有 scored"):
        ensure_queue_eligible(
            status=PaperStatus.CLASSIFIED,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
        )

    with pytest.raises(InvariantViolation, match="特殊桶文献不得进入主队列"):
        ensure_queue_eligible(
            status=PaperStatus.SCORED,
            snapshot=_score(),
            filing_bucket=_review_bucket(),
        )

    with pytest.raises(InvariantViolation, match="低于相关度门槛"):
        ensure_queue_eligible(
            status=PaperStatus.SCORED,
            snapshot=_score(relevance=54.9),
            filing_bucket=_topic_bucket(),
        )


def test_shelvable_requires_classified_special_bucket() -> None:
    ensure_shelvable(status=PaperStatus.CLASSIFIED, filing_bucket=_review_bucket())

    with pytest.raises(InvariantViolation, match="只有 classified"):
        ensure_shelvable(status=PaperStatus.SCORED, filing_bucket=_review_bucket())

    with pytest.raises(InvariantViolation, match="主题桶文献不得进入 shelved"):
        ensure_shelvable(status=PaperStatus.CLASSIFIED, filing_bucket=_topic_bucket())


def test_classification_review_thresholds() -> None:
    assert classification_requires_review(0.69)
    assert classification_requires_review(0.75, 0.70)
    assert not classification_requires_review(0.80, 0.60)
    assert not classification_requires_review(0.70)

    with pytest.raises(InvariantViolation, match="0–1"):
        classification_requires_review(1.2)


def test_low_confidence_must_route_to_review() -> None:
    ensure_low_confidence_routed_to_review(status=PaperStatus.NEEDS_REVIEW, requires_review=True)
    ensure_low_confidence_routed_to_review(status=PaperStatus.QUEUED, requires_review=False)

    with pytest.raises(InvariantViolation, match="needs_review"):
        ensure_low_confidence_routed_to_review(status=PaperStatus.QUEUED, requires_review=True)


def test_push_eligibility_for_topic_and_special_buckets() -> None:
    ensure_push_eligible(
        status=PaperStatus.QUEUED,
        snapshot=_score(relevance=70),
        filing_bucket=_topic_bucket(),
        summary=_summary(),
        primary_tag=_tag_assignment("T01"),
    )

    ensure_push_eligible(
        status=PaperStatus.SHELVED,
        snapshot=_score(relevance=20, priority=None, tier=ScoreTier.LOW),
        filing_bucket=_review_bucket(),
        summary=_summary(),
        primary_tag=_tag_assignment("T01"),
    )

    with pytest.raises(InvariantViolation, match="摘要未通过校验"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
            summary=_summary(status=SummaryValidationStatus.FAILED),
            primary_tag=_tag_assignment("T01"),
        )

    with pytest.raises(InvariantViolation, match="低置信度分类"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
            summary=_summary(),
            primary_tag=_tag_assignment("T01", confidence=0.5),
        )

    with pytest.raises(InvariantViolation, match="必须处于 queued"):
        ensure_push_eligible(
            status=PaperStatus.NEEDS_REVIEW,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
            summary=_summary(),
            primary_tag=_tag_assignment("T01"),
        )

    with pytest.raises(InvariantViolation, match="必须处于 shelved"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(priority=None, tier=ScoreTier.HIGH),
            filing_bucket=_review_bucket(),
            summary=_summary(),
            primary_tag=_tag_assignment("T01"),
        )

    with pytest.raises(InvariantViolation, match="同一篇文献"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
            summary=_summary(paper_id="p002"),
            primary_tag=_tag_assignment("T01"),
        )


def test_static_score_is_frozen_unless_version_change_is_confirmed() -> None:
    previous = _score(static_score=61.5)
    ensure_static_score_frozen(previous=previous, candidate=_score(static_score=61.5))

    with pytest.raises(InvariantViolation, match="已冻结"):
        ensure_static_score_frozen(previous=previous, candidate=_score(static_score=65.0))

    with pytest.raises(InvariantViolation, match="缺少对应版本变更"):
        ensure_static_score_frozen(
            previous=previous,
            candidate=_score(static_score=65.0),
            trigger=RecalculationTrigger.PROFILE_CHANGED,
            confirmed=True,
        )

    ensure_static_score_frozen(
        previous=previous,
        candidate=_score(static_score=65.0, profile_version="1.1"),
        trigger=RecalculationTrigger.PROFILE_CHANGED,
        confirmed=True,
    )

    ensure_static_score_frozen(
        previous=previous,
        candidate=_score(static_score=66.0, config_version="1.1.0"),
        trigger=RecalculationTrigger.CONFIG_CHANGED,
        confirmed=True,
    )

    with pytest.raises(InvariantViolation, match="不能跨文献"):
        ensure_static_score_frozen(
            previous=previous,
            candidate=_score(paper_id="p002", static_score=65.0),
            trigger=RecalculationTrigger.METADATA_CORRECTED,
            confirmed=True,
        )


def test_score_snapshots_are_append_only() -> None:
    history = [_score(score_date=SCORE_DATE)]
    ensure_score_snapshot_append_only(history, _score(score_date=SCORE_DATE + timedelta(days=1)))

    with pytest.raises(InvariantViolation, match="同一天只能有一条"):
        ensure_score_snapshot_append_only(history, _score(score_date=SCORE_DATE))

    with pytest.raises(InvariantViolation, match="不能回填旧日期"):
        ensure_score_snapshot_append_only(
            history, _score(score_date=SCORE_DATE - timedelta(days=1))
        )

    with pytest.raises(InvariantViolation, match="不属于"):
        ensure_score_snapshot_append_only(
            history,
            _score(paper_id="p002", score_date=SCORE_DATE + timedelta(days=1)),
        )


def test_event_history_is_append_only() -> None:
    first = Event(id="e1", event_type=EventType.PAPER_DONE, created_at=NOW)
    second = Event(
        id="e2",
        event_type=EventType.PAPER_ARCHIVED,
        created_at=NOW + timedelta(seconds=1),
    )
    ensure_event_history_append_only([first, second])
    ensure_event_appended([first], second)

    with pytest.raises(InvariantViolation, match="事件 ID 重复"):
        ensure_event_history_append_only([first, first])

    with pytest.raises(InvariantViolation, match="按时间追加"):
        ensure_event_history_append_only([second, first])

    with pytest.raises(InvariantViolation, match="事件 ID 重复"):
        ensure_event_appended([first], first)


def _archive(**overrides: object) -> ArchiveSnapshot:
    values: dict[str, object] = {
        "paper_id": "p001",
        "archived_at": NOW,
        "primary_tag": "T01",
        "filing_bucket_id": "b-t01",
        "profile_version": "1.0",
        "taxonomy_version": "1.0",
        "config_version": "1.0.0",
        "content_hash": INPUT_HASH,
        "rating": 5,
        "note": "值得复现",
    }
    values.update(overrides)
    return ArchiveSnapshot(**values)  # type: ignore[arg-type]


def test_archive_snapshot_is_immutable() -> None:
    original = _archive()
    ensure_archive_snapshot_immutable(original, _archive())

    with pytest.raises(InvariantViolation, match="归档快照不可变"):
        ensure_archive_snapshot_immutable(original, _archive(note="修订"))


def test_tag_assignments_cannot_mix_papers() -> None:
    with pytest.raises(InvariantViolation, match="标签属于"):
        ensure_single_primary_tag([_tag_assignment("T01", paper_id="p001")], paper_id="p002")


def test_secondary_tag_catalog_rules() -> None:
    wrong_level = _taxonomy(tags=(_tag("T01", level=TagLevel.SECONDARY),))
    with pytest.raises(InvariantViolation, match="不是主标签"):
        ensure_tags_match_taxonomy([_tag_assignment("T01")], wrong_level)

    with pytest.raises(InvariantViolation, match="不能作为副标签"):
        ensure_tags_match_taxonomy(
            [
                _tag_assignment("T01"),
                _tag_assignment("S01", role=TagRole.SECONDARY),
            ],
            _taxonomy(tags=(_tag("T01"), _tag("S01"))),
        )

    free_secondary = _tag_assignment("FreeTopic", role=TagRole.SECONDARY)
    ensure_tags_match_taxonomy([_tag_assignment("T01"), free_secondary], _taxonomy())


def test_bucket_assignments_cannot_mix_papers_or_use_inactive_buckets() -> None:
    with pytest.raises(InvariantViolation, match="桶关系属于"):
        ensure_single_filing_bucket([_bucket_assignment("b-t01", paper_id="p001")], paper_id="p002")

    inactive = Bucket(
        id="b-t01",
        name="检索",
        bucket_type=BucketType.TOPIC,
        tag_code="T01",
        active=False,
    )
    with pytest.raises(InvariantViolation, match="已停用"):
        ensure_bucket_references([_bucket_assignment("b-t01")], {"b-t01": inactive})


def test_bucket_catalog_rejects_duplicate_ids_and_topic_codes() -> None:
    first = _topic_bucket()
    same_id = Bucket(id="b-t01", name="另一个桶", bucket_type=BucketType.TOPIC, tag_code="T02")
    with pytest.raises(InvariantViolation, match="桶 ID 必须唯一"):
        ensure_bucket_catalog([first, same_id])

    same_topic = Bucket(id="b-t01-2", name="重复主题", bucket_type=BucketType.TOPIC, tag_code="T01")
    with pytest.raises(InvariantViolation, match="同一主标签只能有一个主题桶"):
        ensure_bucket_catalog([first, same_topic])


def test_classification_review_rejects_invalid_runner_up() -> None:
    with pytest.raises(InvariantViolation, match="次高置信度"):
        classification_requires_review(0.8, 1.2)


def test_push_requires_primary_role_and_threshold() -> None:
    with pytest.raises(InvariantViolation, match="必须提供主标签分配"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(),
            filing_bucket=_topic_bucket(),
            summary=_summary(),
            primary_tag=_tag_assignment("S01", role=TagRole.SECONDARY),
        )

    with pytest.raises(InvariantViolation, match="低于相关度门槛"):
        ensure_push_eligible(
            status=PaperStatus.QUEUED,
            snapshot=_score(relevance=54.9),
            filing_bucket=_topic_bucket(),
            summary=_summary(),
            primary_tag=_tag_assignment("T01"),
        )


def test_static_score_without_previous_snapshot_is_allowed() -> None:
    ensure_static_score_frozen(previous=None, candidate=_score())


def test_score_history_cannot_mix_papers() -> None:
    with pytest.raises(InvariantViolation, match="混入了其他文献"):
        ensure_score_snapshot_append_only(
            [_score(paper_id="p002")],
            _score(paper_id="p001"),
            paper_id="p001",
        )


def test_event_append_rejects_earlier_event() -> None:
    first = Event(id="e2", event_type=EventType.PAPER_DONE, created_at=NOW)
    earlier = Event(
        id="e1",
        event_type=EventType.PAPER_DONE,
        created_at=NOW - timedelta(seconds=1),
    )

    with pytest.raises(InvariantViolation, match="按时间追加"):
        ensure_event_appended([first], earlier)


def test_mixed_paper_lists_are_rejected_without_explicit_id() -> None:
    with pytest.raises(InvariantViolation, match="标签属于"):
        ensure_single_primary_tag(
            [
                _tag_assignment("T01", paper_id="p001"),
                _tag_assignment("S01", role=TagRole.SECONDARY, paper_id="p002"),
            ]
        )

    with pytest.raises(InvariantViolation, match="桶关系属于"):
        ensure_single_filing_bucket(
            [
                _bucket_assignment("b-t01", paper_id="p001"),
                _bucket_assignment("b-review", paper_id="p002"),
            ]
        )
