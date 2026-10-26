"""反馈变化清单与事实概览指标的纯计算验证：固定样例精确断言。"""

from datetime import UTC, date, datetime

from papersteward.reading.insights.aggregate import build_overview, feedback_changes
from papersteward.reading.insights.contracts import (
    FeedbackVersion,
    InsightsProjection,
    PaperProjection,
    PeriodKind,
    PeriodMembership,
    ReadingSourceKind,
    ReadingTime,
    ReadingTimeBasis,
    ResolvedPeriod,
    ReviewMode,
    ScoreFeedbackChange,
    TimePrecision,
)

T0 = datetime(2026, 10, 20, 2, tzinfo=UTC)
T1 = datetime(2026, 10, 22, 3, tzinfo=UTC)
T2 = datetime(2026, 10, 23, 3, tzinfo=UTC)
T3 = datetime(2026, 10, 24, 3, tzinfo=UTC)
T4 = datetime(2026, 10, 25, 3, tzinfo=UTC)


def version(at: datetime, score: int | None, note: str | None, *, initial: bool = False):
    return FeedbackVersion(recorded_at=at, score=score, note=note, is_initial=initial)


def test_initial_feedback_never_counts_as_revision():
    assert feedback_changes("p", [version(T0, 5, "初始", initial=True)]) == []
    assert feedback_changes("p", []) == []


def test_score_and_note_changes_are_detected_independently():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 4, "初始"),
        version(T2, 4, "更新"),
    ]
    changes = feedback_changes("p", versions)
    assert [(c.kind, c.old, c.new, c.recorded_at) for c in changes] == [
        ("score", 5, 4, T1),
        ("note", "初始", "更新", T2),
    ]


def test_clearing_to_null_is_a_real_change():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, None, "初始"),
        version(T2, None, None),
    ]
    changes = feedback_changes("p", versions)
    assert [(c.kind, c.old, c.new) for c in changes] == [
        ("score", 5, None),
        ("note", "初始", None),
    ]


def test_unchanged_rewrite_creates_no_change_event():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 5, "初始"),
        version(T2, 5, "初始"),
    ]
    assert feedback_changes("p", versions) == []


def test_same_timestamp_row_changing_both_fields_records_score_then_note():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 2, "改写"),
    ]
    changes = feedback_changes("p", versions)
    assert [c.kind for c in changes] == ["score", "note"]
    assert all(c.recorded_at == T1 for c in changes)
    assert all(c.paper_id == "p" for c in changes)


# —— build_overview 纯统计验证（固定样例）——————————————————————

TZ = "Asia/Shanghai"
COLLECTED = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)
WEEK = ResolvedPeriod(
    kind=PeriodKind.WEEK,
    timezone=TZ,
    label="2026-W43",
    start_date=date(2026, 10, 19),
    end_date_inclusive=date(2026, 10, 25),
    start_utc=datetime(2026, 10, 18, 16, tzinfo=UTC),
    end_utc_exclusive=CUTOFF,
)
ARCHIVED = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)


def day_reading(day: str, *, basis: str = "declared") -> ReadingTime:
    return ReadingTime(
        precision=TimePrecision.DAY,
        basis=ReadingTimeBasis(basis),
        day=date.fromisoformat(day),
        recorded_at_utc=ARCHIVED,
    )


UNKNOWN_READING = ReadingTime(precision=TimePrecision.UNKNOWN, basis=ReadingTimeBasis.UNKNOWN)


def paper(
    paper_id: str,
    *,
    source: str = "system",
    membership: str = "in_period",
    recorded_in_period: bool = True,
    reading: ReadingTime | None = None,
    score: int | None = 4,
    note: str | None = "备注",
    mode: str | None = "deep",
    tag: str | None = "T01",
    taxonomy_version: str | None = "v1",
    published_on: date | None = date(2024, 5, 1),
    changes: list | None = None,
    feedback: list | None = None,
) -> PaperProjection:
    return PaperProjection(
        paper_id=paper_id,
        archived_at=ARCHIVED,
        source=ReadingSourceKind(source),
        content_available=True,
        reading=reading if reading is not None else day_reading("2026-10-20"),
        membership=PeriodMembership(membership),
        recorded_in_period=recorded_in_period,
        title=f"标题-{paper_id}",
        reading_mode=mode,
        published_on=published_on,
        primary_tag=tag,
        taxonomy_version=taxonomy_version,
        feedback=feedback
        if feedback is not None
        else [
            FeedbackVersion(
                recorded_at=ARCHIVED,
                score=score,
                note=note,
                is_initial=True,
            )
        ],
        changes=changes or [],
    )


def projection(papers: list[PaperProjection], *, kind: str = "week") -> InsightsProjection:
    period = (
        WEEK
        if kind == "week"
        else ResolvedPeriod(kind=PeriodKind.ALL, timezone=TZ, label="all-history")
    )
    return InsightsProjection(
        projection_version="reading-insights.projection/1",
        mode=ReviewMode.AS_RECORDED,
        period=period,
        knowledge_cutoff=CUTOFF,
        collected_at=COLLECTED,
        papers=papers,
    )


def test_two_readings_and_unknown_backfills_are_counted_separately():
    papers = [
        paper("sys-1", score=4, note="有备注"),
        paper("sys-2", score=5, note=None),
        *[paper(f"imp-{i:02d}", source="imported", reading=UNKNOWN_READING) for i in range(50)],
    ]
    metrics = build_overview(projection(papers)).metrics
    assert metrics.readings_total == 2
    assert metrics.readings_system == 2
    assert metrics.readings_imported == 0
    assert metrics.backfills_total == 50
    assert metrics.unassigned_total == 50
    assert metrics.partial_month_total == 0
    assert metrics.all_history_total == 52
    assert metrics.coverage.scored == 2 and metrics.coverage.noted == 1
    assert metrics.coverage.total == 2
    assert metrics.score_distribution.score_4 == 1
    assert metrics.score_distribution.score_5 == 1
    assert len(build_overview(projection(papers)).readings) == 2
    assert len(build_overview(projection(papers)).backfills) == 50


def test_import_declared_in_period_counts_in_both():
    papers = [
        paper(
            "imp-declared",
            source="imported",
            reading=day_reading("2026-10-21"),
            score=3,
        ),
    ]
    overview = build_overview(projection(papers))
    assert overview.metrics.readings_total == 1
    assert overview.metrics.readings_imported == 1
    assert overview.metrics.backfills_total == 1
    assert [p.paper_id for p in overview.readings] == ["imp-declared"]
    assert [p.paper_id for p in overview.backfills] == ["imp-declared"]


def test_null_clear_counts_as_unrated():
    papers = [
        paper(
            "sys-clear",
            feedback=[
                FeedbackVersion(recorded_at=ARCHIVED, score=5, note="x", is_initial=True),
                FeedbackVersion(recorded_at=T2, score=None, note="x"),
            ],
        ),
    ]
    metrics = build_overview(projection(papers)).metrics
    assert metrics.score_distribution.unrated == 1
    assert metrics.coverage.scored == 0
    assert metrics.coverage.noted == 1


def test_changes_are_scoped_to_period_and_papers_deduped():
    inside_p1 = ScoreFeedbackChange(
        kind="score", paper_id="p1", recorded_at=datetime(2026, 10, 21, 3, tzinfo=UTC), old=5, new=3
    )
    outside = ScoreFeedbackChange(
        kind="score", paper_id="p1", recorded_at=datetime(2026, 10, 27, 3, tzinfo=UTC), old=3, new=4
    )
    inside_p2 = ScoreFeedbackChange(
        kind="score", paper_id="p2", recorded_at=datetime(2026, 10, 22, 3, tzinfo=UTC), old=4, new=2
    )
    papers = [
        paper("p1", changes=[inside_p1, outside]),
        paper("p2", changes=[inside_p2]),
    ]
    overview = build_overview(projection(papers))
    stats = overview.metrics.feedback_changes
    assert stats.score_events == 2
    assert stats.note_events == 0
    assert stats.papers_affected == 2
    assert len(overview.changes) == 2


def test_primary_tags_split_by_taxonomy_version_and_missing():
    papers = [
        paper("a", tag="T01", taxonomy_version="v1"),
        paper("b", tag="T01", taxonomy_version="v2"),
        paper("c", tag=None),
    ]
    entries = build_overview(projection(papers)).metrics.primary_tags
    assert [(e.key, e.taxonomy_version, e.count) for e in entries] == [
        ("T01", "v1", 1),
        ("T01", "v2", 1),
        ("", None, 1),
    ]
    assert entries[-1].label == "缺标签"


def test_published_decades_and_reading_modes():
    papers = [
        paper("a", published_on=date(2024, 1, 1), mode="deep"),
        paper("b", published_on=date(1999, 1, 1), mode="skim"),
        paper("c", published_on=None, mode=None),
    ]
    metrics = build_overview(projection(papers)).metrics
    assert [(e.key, e.count) for e in metrics.published_decades] == [
        ("1990s", 1),
        ("2020s", 1),
        ("unknown", 1),
    ]
    assert [(e.key, e.count) for e in metrics.reading_modes] == [
        ("deep", 1),
        ("skim", 1),
        ("unknown", 1),
    ]


def test_tag_filter_narrows_everything():
    papers = [paper("a", tag="T01"), paper("b", tag="T02", score=2, note=None)]
    overview = build_overview(projection(papers), tag="T02")
    metrics = overview.metrics
    assert metrics.all_history_total == 1
    assert metrics.readings_total == 1
    assert metrics.coverage.total == 1
    assert [p.paper_id for p in overview.readings] == ["b"]


def test_all_period_excludes_unknown_dates_from_readings():
    papers = [
        paper("sys", reading=day_reading("1999-01-01")),
        paper("imp", source="imported", reading=UNKNOWN_READING),
    ]
    overview = build_overview(projection(papers, kind="all"))
    metrics = overview.metrics
    assert metrics.readings_total == 1
    assert metrics.unassigned_total == 1
    assert metrics.all_history_total == 2


def test_empty_projection_is_all_zero():
    metrics = build_overview(projection([])).metrics
    assert metrics.readings_total == 0
    assert metrics.backfills_total == 0
    assert metrics.coverage.total == 0
    assert metrics.all_history_total == 0
    assert metrics.score_distribution.unrated == 0
    assert metrics.reading_modes == []
    assert metrics.feedback_changes.score_events == 0


def test_entries_are_sorted_deterministically():
    papers = [
        paper("a", tag="T02"),
        paper("b", tag="T01"),
        paper("c", tag="T01"),
    ]
    entries = build_overview(projection(papers)).metrics.primary_tags
    assert [(e.key, e.count) for e in entries] == [("T01", 2), ("T02", 1)]


def test_asset_events_are_counted_within_period_and_deduped():
    events = [
        ("a", datetime(2026, 10, 20, 3, tzinfo=UTC)),  # 周期内
        ("a", datetime(2026, 10, 22, 3, tzinfo=UTC)),  # 周期内（同资产）
        ("b", datetime(2026, 10, 21, 3, tzinfo=UTC)),  # 周期内
        ("c", datetime(2026, 10, 27, 3, tzinfo=UTC)),  # 周期外
    ]
    metrics = build_overview(projection([paper("p")]), asset_events=events).metrics
    assert metrics.asset_events == 3
    assert metrics.asset_assets == 2


def test_asset_events_default_to_zero():
    metrics = build_overview(projection([paper("p")])).metrics
    assert metrics.asset_events == 0
    assert metrics.asset_assets == 0
