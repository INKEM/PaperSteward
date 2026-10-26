"""事实报告的编制、保存、读取与删除；冻结正文剥离私人备注原文。

生成顺序遵循阶段计划 §8.1：预约运行（submit 已完成）→ 只读会话读取
与汇总（``prepare_report``）→ 事务外算指纹 → ``BEGIN IMMEDIATE`` 短写
保存（``save_report``，调用方持锁）。运行 ID 与输入指纹双重去重，
取消后不落任何报告行。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.errors import InvalidRequestError, NotFoundError
from papersteward.reading.insights.aggregate import build_overview
from papersteward.reading.insights.contracts import (
    REPORT_CONTRACT,
    InsightOverview,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightReportContent,
    InsightsProjection,
    NoteFeedbackChange,
    PaperProjection,
    PeriodKind,
    ReportAssetRef,
    ReportChange,
    ReportDetail,
    ReportFeedbackVersion,
    ReportPaper,
    ReportSummary,
    ResolvedPeriod,
    ReviewMode,
    ScoreFeedbackChange,
)
from papersteward.reading.insights.periods import knowledge_cutoff, resolve_period
from papersteward.reading.insights.projection import build_projection
from papersteward.reading.insights.timeline import workspace_timezone
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import ReadingReportRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ReadingAssetRevisionRow, ReadingAssetRow, ReadingReportRow
from papersteward.storage.repositories import RunRepository


class ReportPreparationCancelled(Exception):
    """只读采集在批次边界停止，不保存半成品。"""


class ReviewSpec(Protocol):
    """回顾参数的最小结构；jobs.ReviewParams 按结构满足。"""

    @property
    def period(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def anchor_date(self) -> date | None: ...

    @property
    def start_date(self) -> date | None: ...

    @property
    def end_date(self) -> date | None: ...


@dataclass(frozen=True)
class PreparedReport:
    """只读阶段产物：写事务里只做序列化与落库，不再触碰源数据。"""

    query: InsightQueryRequest
    period: ResolvedPeriod
    knowledge_cutoff: datetime
    collected_at: datetime
    content: InsightReportContent
    fingerprint: str


def prepare_report(
    session: Session,
    params: ReviewSpec,
    *,
    collected_at: datetime,
    note: Callable[[str], None] | None = None,
    include_asset_bodies: bool = False,
    timezone_name: str | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> PreparedReport:
    """读取记录并汇总事实；只读，不产生任何写入。"""
    timezone = workspace_timezone(session)
    payload: dict[str, object] = {
        "kind": params.period,
        "timezone": timezone_name or timezone.key,
    }
    for name in ("anchor_date", "start_date", "end_date"):
        value = getattr(params, name)
        if value is not None:
            payload[name] = value
    try:
        period_request = InsightPeriodRequest.model_validate(payload)
    except ValueError as exc:
        raise InvalidRequestError(f"报告周期参数无效：{exc}") from None
    query = InsightQueryRequest(
        period=period_request, mode=ReviewMode(params.mode), tag=getattr(params, "tag", None)
    )
    period = resolve_period(period_request)
    cutoff = knowledge_cutoff(period, query.mode, collected_at)
    if note is not None:
        note("reading")

    def checkpoint() -> None:
        if should_abort is not None and should_abort():
            raise ReportPreparationCancelled
        if note is not None:
            note("reading")

    # 论文、反馈、日期与资产共享同一个真实 SQLite 读取快照。
    from papersteward.reading.insights.assets import asset_events

    session.execute(text("BEGIN DEFERRED"))
    try:
        projection = build_projection(
            session, query, collected_at=collected_at, shared_snapshot=True, on_batch=checkpoint
        )
        events = asset_events(session, until=cutoff)
        assets = _asset_refs_at(session, cutoff, include_bodies=include_asset_bodies)
    finally:
        session.rollback()
    checkpoint()
    if note is not None:
        note("summarizing")
    overview = build_overview(projection, tag=query.tag, asset_events=events)
    if period.kind is not PeriodKind.ALL:
        from papersteward.reading.insights.aggregate import _period_asset_events

        active_ids = {asset_id for asset_id, _ in _period_asset_events(events, period)}
        assets = [ref for ref in assets if ref.asset_id in active_ids]
        events = [(asset_id, at) for asset_id, at in events if asset_id in active_ids]
    content = build_report_content(overview, assets=assets)
    fingerprint = report_fingerprint(
        query, period, cutoff, projection, assets=assets, asset_events=events
    )
    return PreparedReport(
        query=query,
        period=period,
        knowledge_cutoff=cutoff,
        collected_at=collected_at,
        content=content,
        fingerprint=fingerprint,
    )


def report_fingerprint(
    query: InsightQueryRequest,
    period: ResolvedPeriod,
    cutoff: datetime,
    projection: InsightsProjection,
    *,
    assets: Sequence[ReportAssetRef] = (),
    asset_events: Sequence[tuple[str, datetime]] = (),
) -> str:
    """输入指纹：规范化请求 + 时间语义 + 全部参与数据的逐篇摘要。

    截止先用于选取有效版本；「周期已结束」标记变化属于语义变化，
    必须生成新快照；不含墙钟生成时间，相同输入去重到既有快照。
    """
    closed = period.end_utc_exclusive is not None and period.end_utc_exclusive <= cutoff
    return content_hash(
        {
            "contract": REPORT_CONTRACT,
            "request": query.model_dump(mode="json"),
            "period_closed": closed,
            "papers": [paper.model_dump(mode="json") for paper in projection.papers],
            "assets": [ref.model_dump(mode="json") for ref in assets],
            "asset_events": sorted(
                (asset_id, recorded_at.isoformat()) for asset_id, recorded_at in asset_events
            ),
        }
    )


def build_report_content(
    overview: InsightOverview, *, assets: Sequence[ReportAssetRef] = ()
) -> InsightReportContent:
    """冻结正文：剥离私人备注原文，只留评分事实与备注有无标记。"""
    return InsightReportContent(
        contract=REPORT_CONTRACT,
        metrics=overview.metrics,
        readings=[_report_paper(paper) for paper in overview.readings],
        backfills=[_report_paper(paper) for paper in overview.backfills],
        changes=[_report_change(change) for change in overview.changes],
        unassigned=[_report_paper(paper) for paper in overview.unassigned],
        assets=list(assets),
    )


def _asset_refs_at(
    session: Session, cutoff: datetime, *, include_bodies: bool
) -> list[ReportAssetRef]:
    """快照时刻的资产引用；正文仅在用户显式勾选时保存副本。"""
    refs: dict[str, ReportAssetRef] = {}
    rows = session.execute(
        select(ReadingAssetRevisionRow, ReadingAssetRow)
        .join(ReadingAssetRow, ReadingAssetRow.id == ReadingAssetRevisionRow.asset_id)
        .where(ReadingAssetRevisionRow.recorded_at < cutoff)
        .order_by(ReadingAssetRevisionRow.asset_id, ReadingAssetRevisionRow.revision)
    )
    for row, asset in rows:
        refs[row.asset_id] = ReportAssetRef(
            asset_id=row.asset_id,
            title=row.title,
            kind=asset.kind,
            status=row.status,
            body=row.body if include_bodies else None,
        )
    return [refs[asset_id] for asset_id in sorted(refs)]


def _report_paper(paper: PaperProjection) -> ReportPaper:
    return ReportPaper(
        paper_id=paper.paper_id,
        archived_at=paper.archived_at,
        source=paper.source,
        content_available=paper.content_available,
        reading=paper.reading,
        membership=paper.membership,
        recorded_in_period=paper.recorded_in_period,
        title=paper.title,
        reading_mode=paper.reading_mode,
        published_on=paper.published_on,
        primary_tag=paper.primary_tag,
        secondary_tags=list(paper.secondary_tags),
        taxonomy_version=paper.taxonomy_version,
        feedback=[
            ReportFeedbackVersion(
                recorded_at=version.recorded_at,
                score=version.score,
                has_note=version.note is not None,
                is_initial=version.is_initial,
            )
            for version in paper.feedback
        ],
    )


def _report_change(change: ScoreFeedbackChange | NoteFeedbackChange) -> ReportChange:
    return ReportChange(
        kind=change.kind,
        paper_id=change.paper_id,
        recorded_at=change.recorded_at,
        old_score=change.old if isinstance(change, ScoreFeedbackChange) else None,
        new_score=change.new if isinstance(change, ScoreFeedbackChange) else None,
    )


def save_report(
    session: Session, prepared: PreparedReport, *, run_id: str, now: datetime
) -> ReadingReportRow | None:
    """短写保存；返回报告行，运行已取消时返回 None（不落任何行）。

    行的 ``run_id`` 不同于本次运行即表示沿用了指纹相同的既有快照。
    """
    repository = ReadingReportRepository(session)
    run = RunRepository(session).get(run_id)
    if run is None or run.status.value != "running":
        return None
    existing = repository.get_by_run(run_id)
    if existing is not None:
        return existing
    deduped = repository.get_by_fingerprint(prepared.fingerprint)
    if deduped is not None:
        # 实质输入未变：返回已有快照并保留其原截止时间与生成时间。
        return deduped
    content_json = prepared.content.model_dump(mode="json")
    period = prepared.period
    row = ReadingReportRow(
        id=uuid4().hex,
        report_kind=period.kind.value,
        period_label=period.label,
        period_start=period.start_date,
        period_end=period.end_date_inclusive,
        timezone=period.timezone,
        review_mode=prepared.query.mode.value,
        knowledge_cutoff=prepared.knowledge_cutoff,
        generated_at=now,
        contract=REPORT_CONTRACT,
        input_fingerprint=prepared.fingerprint,
        content_json=content_json,
        content_hash=content_hash(content_json),
        run_id=run_id,
    )
    repository.add(row)
    return row


def read_report(session: Session, report_id: str) -> ReportDetail:
    """读取冻结快照；哈希不匹配拒绝返回。"""
    row = ReadingReportRepository(session).get(report_id)
    if row is None:
        raise NotFoundError("报告不存在")
    return _row_to_detail(row)


def list_reports(session: Session, *, limit: int, offset: int) -> dict[str, Any]:
    rows, total = ReadingReportRepository(session).list(limit=limit, offset=offset)
    return {
        "items": [_row_to_summary(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def delete_report(session: Session, report_id: str) -> None:
    """只删除报告快照；归档、评分与阅读时间声明不受影响。"""
    if not ReadingReportRepository(session).delete(report_id):
        raise NotFoundError("报告不存在")


def list_series_reports(
    session: Session, summary: ReportSummary, *, limit: int = 50
) -> list[ReportSummary]:
    """同报告系列（同类型+周期+模式）的全部版本，按生成时间倒序。"""
    items = list_reports(session, limit=limit, offset=0)["items"]
    return [
        item
        for item in items
        if isinstance(item, ReportSummary)
        and item.report_kind == summary.report_kind
        and item.period_start == summary.period_start
        and item.period_end == summary.period_end
        and item.mode is summary.mode
    ]


def _row_to_summary(row: ReadingReportRow) -> ReportSummary:
    return ReportSummary(
        report_id=row.id,
        report_kind=row.report_kind,
        period_label=row.period_label,
        period_start=row.period_start,
        period_end=row.period_end,
        timezone=row.timezone,
        mode=ReviewMode(row.review_mode),
        knowledge_cutoff=as_utc(row.knowledge_cutoff),
        generated_at=as_utc(row.generated_at),
        contract=row.contract,
        run_id=row.run_id,
    )


def _row_to_detail(row: ReadingReportRow) -> ReportDetail:
    if content_hash(row.content_json) != row.content_hash:
        raise InvariantViolation("reading_report.hash", "报告内容校验失败")
    content = InsightReportContent.model_validate(row.content_json)
    return ReportDetail(content=content, **_row_to_summary(row).model_dump())


def report_input_status(session: Session, detail: ReportDetail, *, now: datetime) -> dict[str, Any]:
    """按报告原时区、周期和模式比较有效事实；不改动保存内容。

    当时记录的已结束周期仍用原历史截止；现在重新回顾允许后来更正。
    全库库存的增减不使无关周期报告被误标为变化。
    """
    from papersteward.jobs.ui_flow import ReviewParams

    run = RunRepository(session).get(detail.run_id)
    request = run.stats_json.get("request", {}) if run else {}
    review = request.get("review", {}) if isinstance(request, dict) else {}
    payload: dict[str, Any] = {
        "period": detail.report_kind,
        "mode": detail.mode.value,
        "tag": review.get("tag") if isinstance(review, dict) else None,
    }
    if detail.report_kind in {"week", "month"}:
        payload["anchor_date"] = detail.period_start
    elif detail.report_kind == "custom":
        payload.update(start_date=detail.period_start, end_date=detail.period_end)
    prepared = prepare_report(
        session,
        ReviewParams.model_validate(payload),
        collected_at=now,
        timezone_name=detail.timezone,
        include_asset_bodies=any(ref.body is not None for ref in detail.content.assets),
    )
    period_end = prepared.period.end_utc_exclusive
    old_closed = period_end is not None and period_end <= detail.knowledge_cutoff
    new_closed = period_end is not None and period_end <= prepared.knowledge_cutoff
    return {
        "changed": old_closed != new_closed
        or _effective_facts(detail.content) != _effective_facts(prepared.content),
        "compared_cutoff": prepared.knowledge_cutoff,
        "mode": detail.mode,
    }


def _effective_facts(content: InsightReportContent) -> dict[str, Any]:
    payload = content.model_dump(mode="json")
    for field in (
        "collected_at",
        "knowledge_cutoff",
        "all_history_total",
        "source_distribution",
        "date_precision_distribution",
    ):
        payload["metrics"].pop(field, None)
    return payload
