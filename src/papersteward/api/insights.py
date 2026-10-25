"""阅读回顾 JSON 接口与页面共用的查询解析；统计计算在服务层。"""

from datetime import date
from typing import Any

from fastapi import APIRouter, status
from sqlalchemy.orm import Session

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.errors import InvalidRequestError
from papersteward.reading.insights.aggregate import (
    OVERVIEW_DETAIL_DEFAULT_LIMIT,
    OVERVIEW_DETAIL_MAX_LIMIT,
    build_overview,
)
from papersteward.reading.insights.contracts import (
    InsightOverview,
    InsightPeriodRequest,
    InsightQueryRequest,
    PeriodKind,
    ReadTimeInput,
    ReadTimeView,
    ReviewMode,
)
from papersteward.reading.insights.projection import build_projection
from papersteward.reading.insights.timeline import (
    read_time_view,
    record_reading_time,
    workspace_timezone,
)
from papersteward.storage.onboarding import ensure_active
from papersteward.storage.repositories import TaxonomyRepository

router = APIRouter(prefix="/reading-insights", tags=["reading-insights"])


@router.get("/papers/{paper_id}/read-time", response_model=ReadTimeView, summary="查看阅读时间")
def get_read_time(paper_id: str, session: SessionDep) -> Any:
    with domain_errors():
        return read_time_view(session, paper_id)


@router.post(
    "/papers/{paper_id}/read-time",
    response_model=ReadTimeView,
    status_code=status.HTTP_201_CREATED,
    summary="补记或更正阅读时间",
)
def post_read_time(paper_id: str, body: ReadTimeInput, session: SessionDep) -> Any:
    with domain_errors(), session.begin():
        return record_reading_time(
            session,
            paper_id,
            body,
            now=utc_now(),
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )


def parse_overview_query(
    session: Session,
    *,
    kind: str | None,
    anchor_date: str | None,
    start_date: str | None,
    end_date: str | None,
    mode: str | None,
    tag: str | None,
) -> tuple[InsightQueryRequest, str | None]:
    """把 GET 查询参数解析为回顾请求；缺省本周，anchor 取工作区今天。"""
    kind_value = (kind or PeriodKind.WEEK.value).strip()
    if kind_value not in {item.value for item in PeriodKind}:
        raise InvalidRequestError("周期类型无效", details={"field": "kind"})
    mode_value = (mode or ReviewMode.AS_RECORDED.value).strip()
    if mode_value not in {item.value for item in ReviewMode}:
        raise InvalidRequestError("回顾模式无效", details={"field": "mode"})
    anchor = _parse_date_param("anchor_date", anchor_date)
    start = _parse_date_param("start_date", start_date)
    end = _parse_date_param("end_date", end_date)
    if anchor is None and kind_value in (PeriodKind.WEEK.value, PeriodKind.MONTH.value):
        anchor = utc_now().astimezone(workspace_timezone(session)).date()
    payload: dict[str, object] = {
        "kind": kind_value,
        "timezone": workspace_timezone(session).key,
    }
    if anchor is not None:
        payload["anchor_date"] = anchor
    if start is not None:
        payload["start_date"] = start
    if end is not None:
        payload["end_date"] = end
    try:
        period = InsightPeriodRequest.model_validate(payload)
    except ValueError as exc:
        raise InvalidRequestError(f"周期参数无效：{exc}", details={"field": "period"}) from None
    tag_code = (tag or "").strip() or None
    return InsightQueryRequest(period=period, mode=ReviewMode(mode_value)), tag_code


def build_tag_labels(session: Session) -> dict[tuple[str, str], str]:
    """按历史分类版本解析标签名；跨版本同名不合并，解析不到回退 code。"""
    labels: dict[tuple[str, str], str] = {}
    for taxonomy in TaxonomyRepository(session).list():
        for tag in taxonomy.tags:
            labels[(taxonomy.version, tag.code)] = tag.name
    return labels


def finalize_overview(
    session: Session, overview: InsightOverview, *, detail_limit: int
) -> InsightOverview:
    """给主题分布补历史标签名并截断明细；指标始终全量。"""
    if not 1 <= detail_limit <= OVERVIEW_DETAIL_MAX_LIMIT:
        raise InvalidRequestError(
            f"明细条数应在 1–{OVERVIEW_DETAIL_MAX_LIMIT} 之间",
            details={"field": "detail_limit"},
        )
    labels = build_tag_labels(session)
    relabeled = [
        entry
        if not entry.key
        else entry.model_copy(
            update={"label": labels.get((entry.taxonomy_version or "", entry.key), entry.label)}
        )
        for entry in overview.metrics.primary_tags
    ]
    metrics = overview.metrics.model_copy(update={"primary_tags": relabeled})
    return InsightOverview(
        metrics=metrics,
        readings=overview.readings[:detail_limit],
        backfills=overview.backfills[:detail_limit],
        changes=overview.changes[:detail_limit],
        unassigned=overview.unassigned[:detail_limit],
    )


@router.get("/overview", response_model=InsightOverview, summary="事实概览")
def get_overview(
    session: SessionDep,
    kind: str | None = None,
    anchor_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    mode: str | None = None,
    tag: str | None = None,
    detail_limit: int = OVERVIEW_DETAIL_DEFAULT_LIMIT,
) -> Any:
    """实时事实概览；GET 无写入、不创建报告或任务，不检查模型连接。"""
    with domain_errors():
        ensure_active(session, action="reading_insights.overview")
        query, tag_code = parse_overview_query(
            session,
            kind=kind,
            anchor_date=anchor_date,
            start_date=start_date,
            end_date=end_date,
            mode=mode,
            tag=tag,
        )
        projection = build_projection(session, query)
        overview = build_overview(projection, tag=tag_code)
        return finalize_overview(session, overview, detail_limit=detail_limit)


def _parse_date_param(name: str, value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise InvalidRequestError(
            f"{name} 格式无效，请用 YYYY-MM-DD", details={"field": name}
        ) from None
