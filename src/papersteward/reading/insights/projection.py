"""批量只读历史投影：以归档为准，固定知识截止，共享同一真实只读快照。

不调用 ``read_archive`` 逐篇取值，不查当前论文与当前分类法；
查询数随批次增长（每批 3 条 IN 查询），不受页面 ``limit=200`` 限制。
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only

from papersteward.core.entities import ReadingTimeRevision
from papersteward.core.enums import EventType
from papersteward.core.errors import InvariantViolation
from papersteward.reading.archive import verified_archive_content
from papersteward.reading.insights.aggregate import feedback_changes
from papersteward.reading.insights.contracts import (
    PROJECTION_VERSION,
    FeedbackVersion,
    InsightQueryRequest,
    InsightsProjection,
    PaperProjection,
    ReadingSourceKind,
    ReadingTime,
    ReadingTimeBasis,
    ResolvedPeriod,
    TimePrecision,
)
from papersteward.reading.insights.periods import (
    classify_membership,
    knowledge_cutoff,
    local_date,
    recorded_in_period,
    resolve_period,
)
from papersteward.storage.insights import ReadingTimeRevisionRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ArchiveSnapshotRow, EventRow, RatingRow

BATCH_SIZE = 500
_UNKNOWN_READING = ReadingTime(precision=TimePrecision.UNKNOWN, basis=ReadingTimeBasis.UNKNOWN)


def build_projection(
    session: Session,
    request: InsightQueryRequest,
    *,
    collected_at: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> InsightsProjection:
    """按周期与回顾模式批量读取归档事实。

    ``session`` 必须未持有未提交的写事务：这里显式 ``BEGIN DEFERRED``
    开启真实只读快照（sqlite legacy 模式下 SELECT 不保证开启读事务），
    批次遍历共享同一快照，结束后回滚，不引入任何写副作用。
    """
    if batch_size < 1:
        raise InvariantViolation("insights.batch", "投影批次大小必须为正")
    collected = as_utc(collected_at) if collected_at is not None else datetime.now(UTC)
    period = resolve_period(request.period)
    cutoff = knowledge_cutoff(period, request.mode, collected)
    session.execute(text("BEGIN DEFERRED"))
    try:
        papers = _collect_papers(session, period, cutoff, batch_size)
    finally:
        session.rollback()
    return InsightsProjection(
        projection_version=PROJECTION_VERSION,
        mode=request.mode,
        period=period,
        knowledge_cutoff=cutoff,
        collected_at=collected,
        papers=papers,
    )


def _collect_papers(
    session: Session, period: ResolvedPeriod, cutoff: datetime, batch_size: int
) -> list[PaperProjection]:
    """按 paper_id 稳定键集分页遍历；输出与分页顺序一致，保证结果稳定。"""
    papers: list[PaperProjection] = []
    last_id = ""
    while True:
        rows = session.scalars(
            select(ArchiveSnapshotRow)
            .where(
                ArchiveSnapshotRow.paper_id > last_id,
                ArchiveSnapshotRow.archived_at <= cutoff,
            )
            .order_by(ArchiveSnapshotRow.paper_id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        papers.extend(_project_batch(session, rows, period, cutoff))
        if len(rows) < batch_size:
            break
        last_id = rows[-1].paper_id
    return papers


def _project_batch(
    session: Session,
    rows: Iterable[ArchiveSnapshotRow],
    period: ResolvedPeriod,
    cutoff: datetime,
) -> list[PaperProjection]:
    """每批恰好 4 条 IN 查询：快照已由调用方取出，这里补评分、归档事件与日期声明。"""
    ids = [row.paper_id for row in rows]
    ratings_by_paper: dict[str, list[RatingRow]] = {}
    for rating in session.scalars(
        select(RatingRow)
        .options(
            load_only(RatingRow.paper_id, RatingRow.created_at, RatingRow.score, RatingRow.note)
        )
        .where(RatingRow.paper_id.in_(ids), RatingRow.created_at <= cutoff)
        .order_by(RatingRow.paper_id, RatingRow.created_at)
    ):
        ratings_by_paper.setdefault(rating.paper_id, []).append(rating)
    import_evidence: set[str] = set()
    for paper_id, payload in session.execute(
        select(EventRow.paper_id, EventRow.payload_json)
        .where(
            EventRow.event_type == EventType.PAPER_ARCHIVED.value,
            EventRow.paper_id.in_(ids),
            EventRow.created_at <= cutoff,
        )
        .order_by(EventRow.created_at, EventRow.id)
    ).all():
        if (
            isinstance(paper_id, str)
            and isinstance(payload, dict)
            and payload.get("origin") == "import"
        ):
            import_evidence.add(paper_id)
    # 日期声明先受知识截止限制；每篇取截止前最大版本。
    declarations: dict[str, ReadingTimeRevision] = {}
    for paper_id, items in ReadingTimeRevisionRepository(session).list_for_papers(ids).items():
        visible = [item for item in items if as_utc(item.recorded_at) <= cutoff]
        if visible:
            declarations[paper_id] = visible[-1]

    projections = []
    for row in rows:
        archived_at = as_utc(row.archived_at)
        content, available = _verified_content(row)
        reading_content = _dict_field(content, "reading")
        metadata = _dict_field(content, "metadata")
        completed_at = _parse_utc(reading_content.get("completed_at"))
        source = _source_kind(row.paper_id, content, available, completed_at, import_evidence)
        declared = declarations.get(row.paper_id)
        if declared is not None:
            reading = ReadingTime(
                precision=declared.precision,
                basis=declared.basis,
                day=declared.day,
                month=declared.month,
                recorded_at_utc=as_utc(declared.recorded_at),
            )
        else:
            reading = (
                ReadingTime(
                    precision=TimePrecision.DAY,
                    basis=ReadingTimeBasis.SYSTEM_RECORDED,
                    day=local_date(completed_at, period.timezone),
                    recorded_at_utc=completed_at,
                )
                if source is ReadingSourceKind.SYSTEM and completed_at is not None
                else _UNKNOWN_READING
            )
        versions = _feedback_versions(row, ratings_by_paper.get(row.paper_id, []), archived_at)
        projections.append(
            PaperProjection(
                paper_id=row.paper_id,
                archived_at=archived_at,
                source=source,
                content_available=available,
                reading=reading,
                membership=classify_membership(reading, period),
                recorded_in_period=recorded_in_period(archived_at, period),
                title=_optional_text(metadata.get("title")),
                reading_mode=_optional_text(reading_content.get("actual_mode")),
                published_on=_parse_frozen_date(metadata.get("published_at")),
                primary_tag=row.primary_tag,
                secondary_tags=list(row.secondary_tags_json or []),
                taxonomy_version=row.taxonomy_version,
                feedback=versions,
                changes=feedback_changes(row.paper_id, versions),
            )
        )
    return projections


def _source_kind(
    paper_id: str,
    content: dict[str, Any],
    available: bool,
    completed_at: datetime | None,
    import_evidence: set[str],
) -> ReadingSourceKind:
    """来源分流：先读冻结标记，缺内容时只用既有事件证据，否则来源未明。"""
    if available and content.get("origin") == "import":
        return ReadingSourceKind.IMPORTED
    if completed_at is not None:
        return ReadingSourceKind.SYSTEM
    if paper_id in import_evidence:
        return ReadingSourceKind.IMPORTED
    return ReadingSourceKind.UNKNOWN


def _verified_content(row: ArchiveSnapshotRow) -> tuple[dict[str, Any], bool]:
    """单篇坏数据不毁整批投影：哈希不匹配降级为内容缺失。"""
    try:
        return verified_archive_content(row.content_json, row.content_hash)
    except InvariantViolation:
        return {}, False


def _feedback_versions(
    row: ArchiveSnapshotRow, rating_rows: list[RatingRow], archived_at: datetime
) -> list[FeedbackVersion]:
    if rating_rows:
        return [
            FeedbackVersion(
                recorded_at=as_utc(rating.created_at),
                score=rating.score,
                note=rating.note,
                is_initial=index == 0,
            )
            for index, rating in enumerate(rating_rows)
        ]
    # 评分行缺失时以快照列还原初始反馈，与 read_archive 的回退语义一致。
    return [
        FeedbackVersion(recorded_at=archived_at, score=row.rating, note=row.note, is_initial=True)
    ]


def _dict_field(content: dict[str, Any], key: str) -> dict[str, Any]:
    value = content.get(key)
    return value if isinstance(value, dict) else {}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _parse_frozen_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
