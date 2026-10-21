from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

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
    PaperStatus,
    ReadMode,
    RunStatus,
    ScoreTier,
    SpecialBucketKind,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import DomainValidationError

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
INPUT_HASH = "a" * 64


def _provenance() -> Provenance:
    return Provenance(
        model="fake-llm",
        prompt_version="summary/v1",
        generated_at=NOW,
        input_hash=INPUT_HASH,
    )


def _summary(**overrides: object) -> Summary:
    values: dict[str, object] = {
        "paper_id": "p001",
        "summary_100": "提出层次化检索与跨段重排；三个基准上提升 2–4 点；离线聚类延迟较高。",
        "what": "长文档问答的跨段检索",
        "how": "层次化检索与跨段重排",
        "finding": "三个基准提升 2–4 点",
        "limitation": "离线聚类延迟较高",
    }
    values.update(overrides)
    return Summary(**values)  # type: ignore[arg-type]


def test_paper_normalizes_timezone_and_rejects_naive_datetime() -> None:
    shanghai = timezone(timedelta(hours=8))
    paper = Paper(
        id="p001",
        title="Long-document retrieval",
        first_seen_at=datetime(2026, 10, 21, 16, 0, tzinfo=shanghai),
    )

    assert paper.first_seen_at == datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    assert paper.first_seen_at.utcoffset() == timedelta(0)

    with pytest.raises(DomainValidationError, match="必须带时区"):
        Paper(
            id="p002",
            title="Naive timestamp",
            first_seen_at=datetime(2026, 10, 21, 8, 0),  # noqa: DTZ001
        )


def test_entities_are_immutable() -> None:
    paper = Paper(id="p001", title="RAG")

    with pytest.raises(FrozenInstanceError):
        paper.title = "changed"  # type: ignore[misc]


def test_tag_code_and_hierarchy_rules() -> None:
    with pytest.raises(DomainValidationError, match="稳定代码"):
        Tag(code="t01", name="检索", level=TagLevel.PRIMARY)

    with pytest.raises(DomainValidationError, match="主标签不能有父标签"):
        Tag(code="T02", name="RAG", level=TagLevel.PRIMARY, parent_code="T01")

    secondary = Tag(code="S01", name="稀疏检索", level=TagLevel.SECONDARY, parent_code="T02")
    assert secondary.parent_code == "T02"


def test_taxonomy_rejects_duplicate_codes() -> None:
    tag = Tag(code="T01", name="检索", level=TagLevel.PRIMARY)

    with pytest.raises(DomainValidationError, match="标签代码必须唯一"):
        Taxonomy(
            id="tax-1",
            version="1.0",
            status=DocumentStatus.DRAFT,
            content_md="# taxonomy",
            tags=(tag, tag),
        )


def test_confirmed_document_requires_confirmation_time() -> None:
    with pytest.raises(DomainValidationError, match="必须有确认时间"):
        ResearchProfile(
            id="profile-1",
            version="1.0",
            status=DocumentStatus.CONFIRMED,
            content_md="# profile",
        )

    profile = ResearchProfile(
        id="profile-1",
        version="1.0",
        status=DocumentStatus.CONFIRMED,
        content_md="# profile",
        confirmed_at=NOW,
    )
    assert profile.confirmed_at == NOW


def test_bucket_type_requires_matching_owner() -> None:
    with pytest.raises(DomainValidationError, match="主题桶必须绑定主标签"):
        Bucket(id="b-t01", name="检索", bucket_type=BucketType.TOPIC)

    with pytest.raises(DomainValidationError, match="特殊桶必须设置特殊类型"):
        Bucket(id="b-review", name="跨层综述", bucket_type=BucketType.SPECIAL)


def test_summary_counts_non_whitespace_characters() -> None:
    assert _summary(summary_100="字" * 100 + " " * 20).summary_100.startswith("字")

    with pytest.raises(DomainValidationError, match="100 个非空白字符"):
        _summary(summary_100="字" * 101)


def test_passed_summary_requires_provenance_and_evidence() -> None:
    with pytest.raises(DomainValidationError, match="必须带溯源信息"):
        _summary(validation_status=SummaryValidationStatus.PASSED)

    with pytest.raises(DomainValidationError, match="至少有一条证据"):
        _summary(
            validation_status=SummaryValidationStatus.PASSED,
            provenance=_provenance(),
        )

    summary = _summary(
        validation_status=SummaryValidationStatus.PASSED,
        provenance=_provenance(),
        evidence=("原文引文",),
    )
    assert summary.validation_status is SummaryValidationStatus.PASSED


def test_agent_tag_requires_provenance() -> None:
    with pytest.raises(DomainValidationError, match="Agent 生成的标签必须带溯源信息"):
        PaperTag(
            paper_id="p001",
            tag_code="T01",
            role=TagRole.PRIMARY,
            taxonomy_version="1.0",
            assigned_by=AssignedBy.AGENT,
        )


def test_score_snapshot_requires_exactly_one_of_priority_or_tier() -> None:
    values: dict[str, object] = {
        "paper_id": "p001",
        "score_date": NOW.date(),
        "relevance": 70,
        "authority": 50,
        "extra_value": 50,
        "static_score": 61,
        "freshness": 60,
        "hunger": 50,
        "dynamic_score": 56,
        "preference": 50,
        "config_version": "1.0.0",
        "profile_version": "1.0",
        "taxonomy_version": "1.0",
    }

    with pytest.raises(DomainValidationError, match="主队列快照必须有 priority"):
        ScoreSnapshot(**values)  # type: ignore[arg-type]

    with pytest.raises(DomainValidationError, match="主队列快照必须有 priority"):
        ScoreSnapshot(**values, priority=70, tier=ScoreTier.HIGH)  # type: ignore[arg-type]

    topic = ScoreSnapshot(**values, priority=70)  # type: ignore[arg-type]
    special = ScoreSnapshot(**values, tier=ScoreTier.HIGH)  # type: ignore[arg-type]
    assert topic.priority == 70.0
    assert special.tier is ScoreTier.HIGH


def test_run_terminal_status_requires_finished_at() -> None:
    with pytest.raises(DomainValidationError, match="终态运行必须有结束时间"):
        Run(id="r1", run_type="digest", status=RunStatus.SUCCEEDED)

    run = Run(
        id="r1",
        run_type="digest",
        status=RunStatus.SUCCEEDED,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=2),
    )
    assert run.status is RunStatus.SUCCEEDED


def test_paper_status_defaults_to_discovered() -> None:
    assert Paper(id="p001", title="RAG").status is PaperStatus.DISCOVERED


def _score_values() -> dict[str, object]:
    return {
        "paper_id": "p001",
        "score_date": NOW.date(),
        "relevance": 70,
        "authority": 50,
        "extra_value": 50,
        "static_score": 61,
        "freshness": 60,
        "hunger": 50,
        "dynamic_score": 56,
        "preference": 50,
        "config_version": "1.0.0",
        "profile_version": "1.0",
        "taxonomy_version": "1.0",
        "priority": 70,
    }


def _archive_values() -> dict[str, object]:
    return {
        "paper_id": "p001",
        "archived_at": NOW,
        "primary_tag": "T01",
        "filing_bucket_id": "b-t01",
        "profile_version": "1.0",
        "taxonomy_version": "1.0",
        "config_version": "1.0.0",
        "content_hash": INPUT_HASH,
    }


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: Paper(id=" ", title="x"), "paper.id"),
        (lambda: Paper(id="p", title="x", venue=1), "paper.venue"),
        (lambda: Paper(id="p", title="x", first_seen_at="now"), "必须是 datetime"),
        (lambda: Paper(id="p", title="x", status="new"), "paper.status"),
        (lambda: Paper(id="p", title="x", authors="A"), "paper.authors"),
        (lambda: Paper(id="p", title="x", oa_status="open"), "paper.oa_status"),
        (lambda: Tag(code="T01", name="x", level="primary"), "tag.level"),
        (
            lambda: Tag(code="T01", name="x", level=TagLevel.PRIMARY, parent_code="T01"),
            "不能引用自身",
        ),
        (lambda: Bucket(id="b", name="x", bucket_type="topic"), "bucket.bucket_type"),
        (
            lambda: Bucket(
                id="b",
                name="x",
                bucket_type=BucketType.TOPIC,
                tag_code="T01",
                special_kind=SpecialBucketKind.REVIEW,
            ),
            "主题桶不能设置特殊类型",
        ),
        (
            lambda: Bucket(
                id="b",
                name="x",
                bucket_type=BucketType.SPECIAL,
                tag_code="T01",
                special_kind=SpecialBucketKind.REVIEW,
            ),
            "特殊桶不能绑定主标签",
        ),
        (
            lambda: Bucket(
                id="b",
                name="x",
                bucket_type=BucketType.SPECIAL,
                special_kind=SpecialBucketKind.REVIEW,
                quota=True,
            ),
            "必须是整数",
        ),
        (
            lambda: Bucket(
                id="b",
                name="x",
                bucket_type=BucketType.SPECIAL,
                special_kind=SpecialBucketKind.REVIEW,
                quota=-1,
            ),
            "不能为负数",
        ),
        (
            lambda: ResearchProfile(id="p", version="1", status="draft", content_md="x"),
            "profile.status",
        ),
        (
            lambda: ResearchProfile(
                id="p",
                version="1",
                status=DocumentStatus.DRAFT,
                content_md="x",
                confirmed_at=NOW,
            ),
            "草稿不能有确认时间",
        ),
        (lambda: Taxonomy(id="t", version="1", status="draft", content_md="x"), "taxonomy.status"),
        (
            lambda: Taxonomy(
                id="t", version="1", status=DocumentStatus.DRAFT, content_md="x", tags="T01"
            ),
            "必须是 Tag 元组",
        ),
        (
            lambda: Taxonomy(
                id="t",
                version="1",
                status=DocumentStatus.CONFIRMED,
                content_md="x",
                confirmed_at=NOW,
            ),
            "不能没有标签",
        ),
        (
            lambda: Taxonomy(
                id="t", version="1", status=DocumentStatus.DRAFT, content_md="x", tags=("bad",)
            ),
            "必须是 Tag",
        ),
        (
            lambda: PaperTag(paper_id="p", tag_code="T01", role="primary", taxonomy_version="1.0"),
            "paper_tag.role",
        ),
        (
            lambda: PaperTag(
                paper_id="p",
                tag_code="T01",
                role=TagRole.PRIMARY,
                taxonomy_version="1.0",
                confidence="high",
            ),
            "必须是 0–1 之间的数值",
        ),
        (
            lambda: PaperTag(
                paper_id="p",
                tag_code="T01",
                role=TagRole.PRIMARY,
                taxonomy_version="1.0",
                assigned_by="agent",
            ),
            "paper_tag.assigned_by",
        ),
        (
            lambda: PaperTag(
                paper_id="p",
                tag_code="T01",
                role=TagRole.PRIMARY,
                taxonomy_version="1.0",
                assigned_by=AssignedBy.AGENT,
                provenance="trace",
            ),
            "必须是 Provenance 或 null",
        ),
        (lambda: PaperBucket(paper_id="p", bucket_id="b", role="filing"), "paper_bucket.role"),
        (
            lambda: PaperBucket(
                paper_id="p", bucket_id="b", role=BucketRole.FILING, assigned_by="user"
            ),
            "paper_bucket.assigned_by",
        ),
        (
            lambda: _summary(validation_status="passed"),
            "summary.validation_status",
        ),
        (
            lambda: _summary(evidence="not-a-tuple"),
            "summary.evidence",
        ),
        (
            lambda: _summary(
                validation_status=SummaryValidationStatus.PASSED,
                provenance="trace",
                evidence=("quote",),
            ),
            "必须是 Provenance 或 null",
        ),
        (lambda: ScoreSnapshot(**{**_score_values(), "score_date": "today"}), "必须是 date"),
        (lambda: ScoreSnapshot(**{**_score_values(), "relevance": 101}), "必须在 0–100 之间"),
        (
            lambda: ScoreSnapshot(**{**_score_values(), "relevance": "high"}),
            "必须是 0–100 之间的数值",
        ),
        (
            lambda: ScoreSnapshot(**{**_score_values(), "priority": None, "tier": "high"}),
            "必须是 ScoreTier 或 null",
        ),
        (
            lambda: ScoreSnapshot(**_score_values(), provenance="trace"),
            "必须是 Provenance 或 null",
        ),
        (lambda: Event(id="e", event_type="paper.done"), "event.event_type"),
        (lambda: Rating(paper_id="p", score=6), "必须在 1–5 之间"),
        (lambda: Rating(paper_id="p", score=True), "必须是整数或 null"),
        (lambda: Rating(paper_id="p", read_mode="deep"), "ReadMode"),
        (
            lambda: Config(key="k", version="1", value_json={}, updated_by="user"),
            "config.updated_by",
        ),
        (lambda: Config(key="k", version="1", value_json=[]), "必须是 JSON 对象"),
        (lambda: Run(id="r", run_type="t", status="running"), "run.status"),
        (
            lambda: Run(id="r", run_type="t", status=RunStatus.RUNNING, finished_at=NOW),
            "运行中不能有结束时间",
        ),
        (
            lambda: Run(
                id="r",
                run_type="t",
                status=RunStatus.FAILED,
                started_at=NOW,
                finished_at=NOW - timedelta(minutes=1),
            ),
            "不能早于开始时间",
        ),
        (
            lambda: Provenance(model="m", prompt_version="v1", generated_at=NOW, input_hash="abc"),
            "十六进制",
        ),
        (
            lambda: ArchiveSnapshot(
                **_archive_values(), secondary_tags=("S1", "S2", "S3", "S4", "S5", "S6")
            ),
            "最多 5 个副标签",
        ),
        (lambda: ArchiveSnapshot(**_archive_values(), rating=6), "必须在 1–5 之间"),
        (lambda: ArchiveSnapshot(**{**_archive_values(), "content_hash": "abc"}), "十六进制"),
        (lambda: ArchiveSnapshot(**_archive_values(), score_date="today"), "date 或 null"),
        (lambda: ArchiveSnapshot(**_archive_values(), priority=101), "必须在 0–100 之间"),
    ],
)
def test_entity_validation_errors(factory: Callable[[], object], message: str) -> None:
    with pytest.raises(DomainValidationError, match=message):
        factory()


def test_bucket_properties_and_quota() -> None:
    topic = Bucket(id="b-t01", name="检索", bucket_type=BucketType.TOPIC, tag_code="T01", quota=0)
    special = Bucket(
        id="b-review",
        name="跨层综述",
        bucket_type=BucketType.SPECIAL,
        special_kind=SpecialBucketKind.REVIEW,
        quota=2,
    )

    assert topic.is_topic and not topic.is_special
    assert special.is_special and not special.is_topic
    assert special.quota == 2


def test_datetime_score_date_is_normalized_to_date() -> None:
    score = ScoreSnapshot(
        **{**_score_values(), "score_date": datetime(2026, 10, 21, 7, 0, tzinfo=UTC)}
    )
    assert score.score_date == date(2026, 10, 21)


def test_rating_accepts_optional_score_and_read_mode() -> None:
    rating = Rating(paper_id="p001", score=None, note=" ", read_mode=ReadMode.DEEP)

    assert rating.score is None
    assert rating.note is None
    assert rating.read_mode is ReadMode.DEEP


def test_archive_datetime_score_date_is_normalized_to_date() -> None:
    archive = ArchiveSnapshot(
        **_archive_values(), score_date=datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    )
    assert archive.score_date == date(2026, 10, 21)
