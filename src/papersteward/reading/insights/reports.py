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

from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.errors import InvalidRequestError, NotFoundError
from papersteward.reading.insights.contracts import (
    FACTS_ALGORITHM,
    REPORT_CONTRACT,
    InsightOverview,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightReportContent,
    NoteFeedbackChange,
    PaperProjection,
    ReportAssetRef,
    ReportChange,
    ReportContext,
    ReportDetail,
    ReportFeedbackVersion,
    ReportPaper,
    ReportSummary,
    ResolvedPeriod,
    ReviewMode,
    ScoreFeedbackChange,
)
from papersteward.reading.insights.periods import knowledge_cutoff, resolve_period
from papersteward.reading.insights.scope import (
    collect_scope,
    input_fingerprint,
    series_key,
)
from papersteward.reading.insights.timeline import workspace_timezone
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import ReadingReportRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ReadingReportRow
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
    context: ReportContext


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

    selected = collect_scope(
        session,
        query,
        collected_at=collected_at,
        include_asset_bodies=include_asset_bodies,
        on_batch=checkpoint,
    )
    checkpoint()
    if note is not None:
        note("summarizing")
    return PreparedReport(
        query=selected.context.normalized_request or query,
        period=period,
        knowledge_cutoff=cutoff,
        collected_at=collected_at,
        content=build_report_content(selected.overview, assets=selected.overview.assets),
        fingerprint=input_fingerprint(selected.context),
        context=selected.context,
    )


# 所有调用（含纯逻辑检查）使用同一种清单指纹，不另保留全库／专题两套算法。
report_fingerprint = input_fingerprint


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
    repository.save_context(
        row.id, prepared.context.model_dump(mode="json"), series_key(prepared.context)
    )
    return row


def read_report(session: Session, report_id: str) -> ReportDetail:
    """读取冻结快照；哈希不匹配拒绝返回。"""
    row = ReadingReportRepository(session).get(report_id)
    if row is None:
        raise NotFoundError("报告不存在")
    if content_hash(row.content_json) != row.content_hash:
        raise InvariantViolation("reading_report.hash", "报告内容校验失败")
    return _row_to_detail(row, _context_for_report(session, row))


def list_reports(session: Session, *, limit: int, offset: int) -> dict[str, Any]:
    rows, total = ReadingReportRepository(session).list(limit=limit, offset=offset)
    contexts = ReadingReportRepository(session).contexts_for_reports([row.id for row in rows])
    return {
        "items": [_row_to_summary(row, _parse_context(contexts.get(row.id))) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def delete_report(session: Session, report_id: str) -> None:
    """只删除报告快照；归档、评分与阅读时间声明不受影响。"""
    if not ReadingReportRepository(session).delete(report_id):
        raise NotFoundError("报告不存在")


def list_series_reports(
    session: Session, summary: ReportSummary, *, limit: int = 50, offset: int = 0
) -> list[ReportSummary]:
    if limit < 1 or offset < 0:
        raise InvalidRequestError("系列分页参数无效")
    row = ReadingReportRepository(session).get(summary.report_id)
    if row is None:
        raise NotFoundError("报告不存在")
    context = _context_for_report(session, row)
    key = series_key(context)
    if key is None:
        return []
    repository = ReadingReportRepository(session)
    rows = repository.list_series(key, limit=limit, offset=offset)
    contexts = repository.contexts_for_reports([item.id for item in rows])
    return [_row_to_summary(item, _parse_context(contexts.get(item.id))) for item in rows]


def ensure_same_series(base: ReportDetail, other: ReportDetail) -> None:
    a = series_key(base.context) if base.context else None
    b = series_key(other.context) if other.context else None
    if a is None or b is None or a != b:
        raise InvalidRequestError(
            "这两份报告的周期、主题、时区、模式或统计口径不同或未知，无法比较；请选择同系列版本"
        )


def _parse_context(payload: dict[str, Any] | None) -> ReportContext | None:
    return ReportContext.model_validate(payload) if payload is not None else None


def _context_for_report(session: Session, row: ReadingReportRow) -> ReportContext:
    context = _parse_context(
        ReadingReportRepository(session).contexts_for_reports([row.id]).get(row.id)
    )
    if context is not None:
        return context
    # 兼容手工导入的旧行；GET 不补写。正式库由 0024 一次性保存旁路上下文。
    from papersteward.reading.insights.legacy_context import legacy_report_context

    run = RunRepository(session).get(row.run_id)
    return legacy_report_context(
        row.content_json,
        run.stats_json.get("request") if run else None,
        timezone=row.timezone,
    )


def _row_to_summary(row: ReadingReportRow, context: ReportContext | None = None) -> ReportSummary:
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
        context=context,
    )


def _row_to_detail(row: ReadingReportRow, context: ReportContext) -> ReportDetail:
    if content_hash(row.content_json) != row.content_hash:
        raise InvariantViolation("reading_report.hash", "报告内容校验失败")
    content = InsightReportContent.model_validate(row.content_json)
    return ReportDetail(content=content, **_row_to_summary(row, context).model_dump())


def report_input_status(session: Session, detail: ReportDetail, *, now: datetime) -> dict[str, Any]:
    """按报告原时区、周期和模式比较有效事实；不改动保存内容。

    当时记录的已结束周期仍用原历史截止；现在重新回顾允许后来更正。
    全库库存的增减不使无关周期报告被误标为变化。
    """
    context = detail.context
    if (
        context is None
        or context.status != "complete"
        or context.normalized_request is None
        or context.scope.algorithm_version != FACTS_ALGORITHM
    ):
        return {
            "changed": None,
            "status": "unknown",
            "compared_cutoff": None,
            "mode": detail.mode,
            "reason": "旧报告的主题或统计口径无法完整还原，不能判断变化；原快照仍可阅读和导出",
        }
    selected = collect_scope(
        session,
        context.normalized_request,
        collected_at=now,
        include_asset_bodies=context.include_asset_bodies is True,
    )
    return {
        "changed": input_fingerprint(context) != input_fingerprint(selected.context),
        "status": "known",
        "compared_cutoff": selected.context.scope.knowledge_cutoff,
        "mode": detail.mode,
    }


def _effective_facts(content: InsightReportContent) -> dict[str, Any]:
    """旧审计探针的事实视图；业务去重和变化提示已统一使用输入清单。"""
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
