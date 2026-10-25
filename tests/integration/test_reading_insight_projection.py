"""历史分析投影的集成验证：截止选择、来源分流、批量查询与结果稳定。

固定样例：工作区时区 Asia/Shanghai；2026-10-19..25 这一周内一篇系统
阅读（多次修订）、若干导入归档与缺失内容归档，外加 250 篇批量数据。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from papersteward.core.entities import ArchiveSnapshot, Event, Paper, Rating
from papersteward.core.enums import EventType, PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.reading.archive import read_archive
from papersteward.reading.insights.contracts import (
    InsightPeriodRequest,
    InsightQueryRequest,
    PeriodMembership,
    ReadingSourceKind,
    ReadingTimeBasis,
    ReviewMode,
    TimePrecision,
)
from papersteward.reading.insights.projection import build_projection
from papersteward.storage import session_scope
from papersteward.storage.delivery import content_hash
from papersteward.storage.mappers import (
    archive_snapshot_to_row,
    event_to_row,
    paper_to_row,
    rating_to_row,
)

TZ = "Asia/Shanghai"
T_DONE = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
COLLECTED = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
# 知识截止（当时记录、周已结束）：2026-10-25T16:00Z（上海 10-26 00:00）。
CUTOFF = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)
REVISIONS: Sequence[tuple[datetime, int | None, str | None]] = [
    (T_DONE, 5, "初始"),
    (datetime(2026, 10, 22, 3, tzinfo=UTC), 4, "初始"),
    (datetime(2026, 10, 23, 3, tzinfo=UTC), 4, "初始"),  # 值未变的重复写入
    (datetime(2026, 10, 24, 3, tzinfo=UTC), 4, "更新"),
    (datetime(2026, 10, 27, 3, tzinfo=UTC), None, "更新"),  # 知识截止之后
]


def seed_paper(session, paper_id: str, *, first_seen_at: datetime) -> None:
    """当前论文标题与归档冻结标题刻意不同，用于验证投影不回填当前值。

    显式 flush 保证父表先落库；autoflush=False 下跨表插入顺序需自行保证。
    """
    session.add(
        paper_to_row(
            Paper(
                paper_id,
                f"当前标题-{paper_id}",
                status=PaperStatus.ARCHIVED,
                first_seen_at=first_seen_at,
            )
        )
    )
    session.flush()


def system_content(*, title: str, completed_at: datetime, read_mode: str = "deep") -> dict:
    return {
        "metadata": {"title": title, "published_at": "2026-01-05T00:00:00+00:00"},
        "reading": {"completed_at": completed_at.isoformat(), "actual_mode": read_mode},
        "initial_feedback": {"score": None, "note": None},
    }


def import_content(*, title: str) -> dict:
    return {"origin": "import", "metadata": {"title": title}}


def seed_archive(
    session,
    paper_id: str,
    *,
    archived_at: datetime,
    content: dict,
    rating_col: int | None = None,
    note_col: str | None = None,
    primary_tag: str | None = "T01",
    ratings: Sequence[tuple[datetime, int | None, str | None]] = (),
    archived_event_origin: str | None = None,
    content_hash_override: str | None = None,
) -> None:
    payload: dict = {"content_hash": "0" * 64}
    if archived_event_origin is not None:
        payload["origin"] = archived_event_origin
    session.add(
        archive_snapshot_to_row(
            ArchiveSnapshot(
                paper_id=paper_id,
                archived_at=archived_at,
                primary_tag=primary_tag,
                filing_bucket_id=None,
                profile_version="pv-test",
                taxonomy_version="tv-test",
                config_version="cv-test",
                content_hash=content_hash_override or content_hash(content),
                content_json=content,
                rating=rating_col,
                note=note_col,
            )
        )
    )
    for created_at, score, note in ratings:
        session.add(
            rating_to_row(Rating(paper_id=paper_id, score=score, note=note, created_at=created_at))
        )
    session.add(
        event_to_row(Event(uuid4().hex, EventType.PAPER_ARCHIVED, archived_at, paper_id, payload))
    )
    # autoflush=False 下同事务内的后续查询需要显式 flush 才可见。
    session.flush()


def seed_week_scenario(factory) -> None:
    with session_scope(factory) as session:
        seed_paper(session, "sys-week", first_seen_at=T_DONE)
        seed_archive(
            session,
            "sys-week",
            archived_at=T_DONE,
            content=system_content(title="冻结标题", completed_at=T_DONE),
            rating_col=5,
            note_col="初始",
            ratings=REVISIONS,
        )


def week_request(mode: ReviewMode = ReviewMode.AS_RECORDED) -> InsightQueryRequest:
    return InsightQueryRequest(
        period=InsightPeriodRequest.model_validate(
            {"kind": "week", "timezone": TZ, "anchor_date": "2026-10-22"}
        ),
        mode=mode,
    )


def month_october_request(mode: ReviewMode = ReviewMode.AS_RECORDED) -> InsightQueryRequest:
    return InsightQueryRequest(
        period=InsightPeriodRequest.model_validate(
            {"kind": "month", "timezone": TZ, "anchor_date": "2026-10-15"}
        ),
        mode=mode,
    )


def test_projection_selects_feedback_by_knowledge_cutoff(insights_factory):
    seed_week_scenario(insights_factory)
    with session_scope(insights_factory) as session:
        projection = build_projection(session, week_request(), collected_at=COLLECTED)
    assert projection.knowledge_cutoff == CUTOFF
    assert projection.period.label == "2026-W43"
    assert [p.paper_id for p in projection.papers] == ["sys-week"]
    paper = projection.papers[0]
    assert paper.source is ReadingSourceKind.SYSTEM
    assert paper.reading.precision is TimePrecision.DAY
    assert paper.reading.basis is ReadingTimeBasis.SYSTEM_RECORDED
    assert paper.reading.day == date(2026, 10, 20)  # UTC 02:00 → 上海当日
    assert paper.reading.recorded_at_utc == T_DONE
    assert paper.membership is PeriodMembership.IN_PERIOD
    assert paper.recorded_in_period
    assert paper.title == "冻结标题"
    assert paper.reading_mode == "deep"
    assert paper.published_on == date(2026, 1, 5)
    assert paper.primary_tag == "T01"
    assert [(v.score, v.note, v.is_initial) for v in paper.feedback] == [
        (5, "初始", True),
        (4, "初始", False),
        (4, "初始", False),
        (4, "更新", False),
    ]
    assert [(c.kind, c.old, c.new) for c in paper.changes] == [
        ("score", 5, 4),
        ("note", "初始", "更新"),
    ]


def test_revised_now_includes_late_revision_and_null_clearing(insights_factory):
    seed_week_scenario(insights_factory)
    collected = datetime(2026, 10, 28, 3, tzinfo=UTC)
    with session_scope(insights_factory) as session:
        projection = build_projection(
            session, week_request(ReviewMode.REVISED_NOW), collected_at=collected
        )
    assert projection.knowledge_cutoff == collected
    paper = projection.papers[0]
    assert [v.score for v in paper.feedback] == [5, 4, 4, 4, None]
    assert [(c.kind, c.old, c.new) for c in paper.changes] == [
        ("score", 5, 4),
        ("note", "初始", "更新"),
        ("score", 4, None),
    ]


def test_import_archives_route_by_frozen_marker_and_event(insights_factory):
    factory = insights_factory
    t_import = datetime(2026, 10, 22, 1, tzinfo=UTC)
    t_old = datetime(2026, 9, 10, 0, tzinfo=UTC)
    with session_scope(factory) as session:
        seed_paper(session, "imp-marker", first_seen_at=t_import)
        seed_archive(
            session,
            "imp-marker",
            archived_at=t_import,
            content=import_content(title="导入A"),
            rating_col=4,
            primary_tag=None,
            ratings=[(t_import, 4, None)],
        )
        seed_paper(session, "imp-event", first_seen_at=t_old)
        seed_archive(
            session,
            "imp-event",
            archived_at=t_old,
            content={},
            rating_col=2,
            ratings=[(t_old, 2, None)],
            archived_event_origin="import",
        )
        seed_paper(session, "done-lost", first_seen_at=t_old)
        seed_archive(
            session,
            "done-lost",
            archived_at=t_old,
            content={},
            rating_col=3,
            ratings=[(t_old, 3, None)],
        )

    all_request = InsightQueryRequest(
        period=InsightPeriodRequest.model_validate({"kind": "all", "timezone": TZ})
    )
    with session_scope(factory) as session:
        projection = build_projection(session, all_request, collected_at=COLLECTED)
    by_id = {p.paper_id: p for p in projection.papers}
    assert set(by_id) == {"imp-marker", "imp-event", "done-lost"}
    assert by_id["imp-marker"].source is ReadingSourceKind.IMPORTED
    assert by_id["imp-marker"].content_available
    assert by_id["imp-marker"].reading.precision is TimePrecision.UNKNOWN
    assert by_id["imp-marker"].primary_tag is None
    # 内容缺失时只依赖既有事件证据；无证据即来源未明，不借当前论文回填。
    assert by_id["imp-event"].content_available is False
    assert by_id["imp-event"].source is ReadingSourceKind.IMPORTED
    assert by_id["imp-event"].title is None
    assert by_id["done-lost"].content_available is False
    assert by_id["done-lost"].source is ReadingSourceKind.UNKNOWN

    with session_scope(factory) as session:
        october = build_projection(session, month_october_request(), collected_at=COLLECTED)
    assert all(
        p.membership is PeriodMembership.UNASSIGNED_UNKNOWN
        and p.source is not (ReadingSourceKind.SYSTEM)
        for p in october.papers
    )
    october_by_id = {p.paper_id: p for p in october.papers}
    assert october_by_id["imp-marker"].recorded_in_period
    assert not october_by_id["imp-event"].recorded_in_period


def test_tampered_content_degrades_but_read_archive_still_raises(insights_factory):
    factory = insights_factory
    t = datetime(2026, 10, 21, 3, tzinfo=UTC)
    with session_scope(factory) as session:
        seed_paper(session, "bad-hash", first_seen_at=t)
        seed_archive(
            session,
            "bad-hash",
            archived_at=t,
            content=system_content(title="冻结标题", completed_at=t),
            rating_col=5,
            note_col="初始",
            ratings=[(t, 5, "初始")],
            content_hash_override="f" * 64,
        )
    with session_scope(factory) as session:
        projection = build_projection(session, week_request(), collected_at=COLLECTED)
    paper = projection.papers[0]
    assert paper.content_available is False
    assert paper.source is ReadingSourceKind.UNKNOWN
    assert paper.title is None
    assert paper.reading.precision is TimePrecision.UNKNOWN
    # 评分行仍可核查；初始反馈回退到快照列。
    assert [(v.score, v.note, v.is_initial) for v in paper.feedback] == [(5, "初始", True)]
    # 既有单篇读取保持拒绝语义，不被投影的降级行为削弱。
    with session_scope(factory) as session, pytest.raises(InvariantViolation):
        read_archive(session, "bad-hash")


def test_full_coverage_beyond_200_with_batched_queries(insights_factory, migrated_engine):
    factory = insights_factory
    base = datetime(2026, 10, 1, 0, tzinfo=UTC)
    with session_scope(factory) as session:
        for index in range(250):
            paper_id = f"bulk-{index:03d}"
            archived_at = base + timedelta(hours=index)
            seed_paper(session, paper_id, first_seen_at=archived_at)
            seed_archive(
                session,
                paper_id,
                archived_at=archived_at,
                content=import_content(title=paper_id),
                rating_col=3,
                ratings=[(archived_at, 3, None)],
                archived_event_origin="import",
            )

    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        with session_scope(factory) as session:
            projection = build_projection(
                session, month_october_request(), collected_at=COLLECTED, batch_size=100
            )
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)

    # 250 篇全量覆盖，不受页面 limit=200 限制。
    assert len(projection.papers) == 250
    assert all(p.source is ReadingSourceKind.IMPORTED for p in projection.papers)
    assert all(p.membership is PeriodMembership.UNASSIGNED_UNKNOWN for p in projection.papers)
    assert all(p.recorded_in_period for p in projection.papers)
    # 查询数随批次增长：100+100+50 三批即止，每批 4 条 IN 查询，无逐篇查询。
    snapshot_selects = [s for s in statements if "FROM archive_snapshot" in s]
    rating_selects = [s for s in statements if "FROM rating" in s]
    event_selects = [s for s in statements if "FROM event" in s]
    revision_selects = [s for s in statements if "FROM reading_time_revision" in s]
    assert len(snapshot_selects) == 3
    assert len(rating_selects) == len(snapshot_selects)
    assert len(event_selects) == len(snapshot_selects)
    assert len(revision_selects) == len(snapshot_selects)
    assert all(" IN " in s for s in rating_selects)


def test_declared_reading_time_overrides_default_by_cutoff(insights_factory):
    factory = insights_factory
    t_import = datetime(2026, 10, 20, 1, tzinfo=UTC)
    declared_late = datetime(2026, 10, 27, 3, tzinfo=UTC)  # 知识截止之后
    with session_scope(factory) as session:
        seed_paper(session, "imp-declared", first_seen_at=t_import)
        seed_archive(
            session,
            "imp-declared",
            archived_at=t_import,
            content=import_content(title="声明月份"),
            rating_col=4,
            ratings=[(t_import, 4, None)],
        )
        from papersteward.core.entities import ReadingTimeRevision
        from papersteward.core.enums import ReadingTimeBasis, TimePrecision
        from papersteward.storage.insights import ReadingTimeRevisionRepository

        repository = ReadingTimeRevisionRepository(session)
        repository.add(
            ReadingTimeRevision(
                paper_id="imp-declared",
                revision=1,
                recorded_at=t_import,
                precision=TimePrecision.MONTH,
                basis=ReadingTimeBasis.DECLARED,
                month="2026-10",
                idempotency_key="decl-1",
            )
        )
        repository.add(
            ReadingTimeRevision(
                paper_id="imp-declared",
                revision=2,
                recorded_at=declared_late,
                precision=TimePrecision.DAY,
                basis=ReadingTimeBasis.DECLARED,
                day=date(2026, 10, 21),
                idempotency_key="decl-2",
            )
        )
    # 当时记录：只可见截止前的月份声明；10 月对周周期只算部分覆盖。
    with session_scope(factory) as session:
        month_view = build_projection(
            session,
            month_october_request(),
            collected_at=COLLECTED,
        )
        week_view = build_projection(session, week_request(), collected_at=COLLECTED)
    month_paper = {p.paper_id: p for p in month_view.papers}["imp-declared"]
    assert month_paper.reading.precision.value == "month"
    assert month_paper.reading.basis.value == "declared"
    assert month_paper.reading.month == "2026-10"
    assert month_paper.membership is PeriodMembership.IN_PERIOD
    week_paper = {p.paper_id: p for p in week_view.papers}["imp-declared"]
    assert week_paper.membership is PeriodMembership.UNASSIGNED_PARTIAL_MONTH

    # 现在重新回顾：截止后的精确日声明生效。
    with session_scope(factory) as session:
        revised = build_projection(
            session,
            month_october_request(ReviewMode.REVISED_NOW),
            collected_at=datetime(2026, 10, 28, 4, tzinfo=UTC),
        )
    revised_paper = {p.paper_id: p for p in revised.papers}["imp-declared"]
    assert revised_paper.reading.precision.value == "day"
    assert str(revised_paper.reading.day) == "2026-10-21"


def test_declared_unknown_clears_system_reading_date(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_week_scenario_data(session)
    with session_scope(factory) as session:
        from papersteward.core.entities import ReadingTimeRevision
        from papersteward.core.enums import ReadingTimeBasis, TimePrecision
        from papersteward.storage.insights import ReadingTimeRevisionRepository

        ReadingTimeRevisionRepository(session).add(
            ReadingTimeRevision(
                paper_id="sys-week",
                revision=1,
                recorded_at=datetime(2026, 10, 23, 3, tzinfo=UTC),
                precision=TimePrecision.UNKNOWN,
                basis=ReadingTimeBasis.DECLARED,
                idempotency_key="clear-1",
            )
        )
    with session_scope(factory) as session:
        projection = build_projection(session, week_request(), collected_at=COLLECTED)
    paper = projection.papers[0]
    assert paper.reading.precision.value == "unknown"
    assert paper.reading.basis.value == "declared"
    assert paper.membership is PeriodMembership.UNASSIGNED_UNKNOWN


def seed_week_scenario_data(session) -> None:
    seed_paper(session, "sys-week", first_seen_at=T_DONE)
    seed_archive(
        session,
        "sys-week",
        archived_at=T_DONE,
        content=system_content(title="冻结标题", completed_at=T_DONE),
        rating_col=5,
        note_col="初始",
        ratings=REVISIONS,
    )


def test_projection_is_stable_for_same_inputs(insights_factory):
    seed_week_scenario(insights_factory)
    t_import = datetime(2026, 10, 22, 1, tzinfo=UTC)
    with session_scope(insights_factory) as session:
        seed_paper(session, "imp-marker", first_seen_at=t_import)
        seed_archive(
            session,
            "imp-marker",
            archived_at=t_import,
            content=import_content(title="导入A"),
            rating_col=4,
            primary_tag=None,
            ratings=[(t_import, 4, None)],
        )
    with session_scope(insights_factory) as session:
        first = build_projection(session, week_request(), collected_at=COLLECTED)
    with session_scope(insights_factory) as session:
        second = build_projection(session, week_request(), collected_at=COLLECTED)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
