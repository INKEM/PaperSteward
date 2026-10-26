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
)
from papersteward.reading.insights.assets import (
    append_revision,
    assets_for_paper,
    create_asset,
    delete_asset,
    delete_preview,
    get_asset_revision_detail,
    get_asset_view,
    list_assets,
)
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    AssetRevisionDetail,
    AssetRevisionInput,
    AssetView,
    InsightOverview,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightsModel,
    PeriodKind,
    ReadTimeInput,
    ReadTimeView,
    ReviewMode,
)
from papersteward.reading.insights.timeline import (
    read_time_view,
    record_reading_time,
    workspace_timezone,
)
from papersteward.storage.onboarding import ensure_active
from papersteward.storage.repositories import TaxonomyRepository
from papersteward.storage.tx import write_tx

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
    with domain_errors(), write_tx(session):
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
    return InsightQueryRequest(period=period, mode=ReviewMode(mode_value), tag=tag_code), tag_code


def build_tag_labels(session: Session) -> dict[tuple[str, str], str]:
    """按历史分类版本解析标签名；跨版本同名不合并，解析不到回退 code。"""
    labels: dict[tuple[str, str], str] = {}
    for taxonomy in TaxonomyRepository(session).list():
        for tag in taxonomy.tags:
            labels[(taxonomy.version, tag.code)] = tag.name
    return labels


def finalize_overview(
    session: Session, overview: InsightOverview, *, detail_limit: int | None
) -> InsightOverview:
    """给主题分布补历史标签名并截断明细；指标始终全量。"""
    if detail_limit is not None and not 1 <= detail_limit <= OVERVIEW_DETAIL_MAX_LIMIT:
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
        assets=overview.assets,
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
        from papersteward.reading.insights.overview import collect_overview

        overview = collect_overview(session, query)
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


# —— 报告快照 JSON 接口 ————————————————————————————————————————

from uuid import uuid4  # noqa: E402

from fastapi import Request, Response  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from pydantic import Field  # noqa: E402

from papersteward.api.workspace_runtime import resolve_workspace  # noqa: E402
from papersteward.jobs.ui_flow import FlowRequest, ReviewParams  # noqa: E402
from papersteward.reading.insights.contracts import (  # noqa: E402
    ReportDetail,
)
from papersteward.reading.insights.export import ReportExportFormat, export_report  # noqa: E402
from papersteward.reading.insights.reports import (  # noqa: E402
    delete_report,
    list_reports,
    read_report,
)


class ReportSubmitInput(InsightsModel):
    """提交回顾报告任务；周期与模式校验由 ReviewParams 承担。"""

    review: ReviewParams
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def submit_review(request: Request, body: ReportSubmitInput) -> dict[str, str]:
    """构建 ui.review 任务并提交到当前工作区的单 worker 队列。"""
    run_id = body.run_id or uuid4().hex
    spec = FlowRequest(run_id=run_id, kind="review", review=body.review)
    resolve_workspace(request).ui_flows.submit(spec)
    return {"run_id": run_id, "run_url": f"/ui/runs/{run_id}"}


@router.post(
    "/reports",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交阅读回顾报告任务",
)
def post_report(body: ReportSubmitInput, request: Request, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        return submit_review(request, body)


@router.get("/reports", summary="报告列表")
def get_reports(session: SessionDep, limit: int = 20, offset: int = 0) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        if not 1 <= limit <= 200 or offset < 0:
            raise InvalidRequestError("分页参数非法", details={"field": "limit"})
        return list_reports(session, limit=limit, offset=offset)


@router.get("/reports/{report_id}", response_model=ReportDetail, summary="报告详情")
def get_report_detail(report_id: str, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        return read_report(session, report_id)


@router.get(
    "/reports/{report_id}/export",
    response_class=PlainTextResponse,
    summary="从保存快照导出报告",
)
def get_report_export(
    report_id: str,
    session: SessionDep,
    format: ReportExportFormat = "markdown",
    include_private: bool = False,
) -> Response:
    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        detail = read_report(session, report_id)
        return PlainTextResponse(export_report(detail, format, include_private=include_private))


@router.delete("/reports/{report_id}", summary="删除报告快照")
def delete_report_route(report_id: str, session: SessionDep) -> Any:
    with domain_errors(), session.begin():
        ensure_active(session, action="reading_insights.report")
        delete_report(session, report_id)
        return {"deleted": True}


# —— 手工资产 JSON 接口 ————————————————————————————————————————


@router.get("/assets", summary="查找手工资产")
def get_assets(
    session: SessionDep,
    kind: str | None = None,
    status: str | None = None,
    paper_id: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.assets")
        if not 1 <= limit <= 200 or offset < 0:
            raise InvalidRequestError("分页参数非法", details={"field": "limit"})
        return list_assets(
            session,
            kind=kind,
            status=status,
            paper_id=paper_id,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )


@router.post(
    "/assets",
    response_model=AssetView,
    status_code=status.HTTP_201_CREATED,
    summary="创建手工资产",
)
def post_asset(body: AssetCreateInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        ensure_active(session, action="reading_insights.assets")
        return create_asset(session, body, now=utc_now())


@router.get("/assets/{asset_id}", response_model=AssetView, summary="资产当前版本与历史")
def get_asset_detail(asset_id: str, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.assets")
        return get_asset_view(session, asset_id)


@router.get(
    "/assets/{asset_id}/revisions/{revision_number}",
    response_model=AssetRevisionDetail,
    summary="逐版本读取历史正文与来源状态",
)
def get_asset_revision(asset_id: str, revision_number: int, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.assets")
        return get_asset_revision_detail(session, asset_id, revision_number)


@router.get("/assets/{asset_id}/papers", summary="引用某篇论文的资产")
def get_assets_for_paper(paper_id: str, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.assets")
        return {"items": assets_for_paper(session, paper_id)}


@router.post(
    "/assets/{asset_id}/revisions",
    response_model=AssetView,
    status_code=status.HTTP_201_CREATED,
    summary="带预期版本追加资产修订",
)
def post_asset_revision(asset_id: str, body: AssetRevisionInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        ensure_active(session, action="reading_insights.assets")
        return append_revision(session, asset_id, body, now=utc_now())


@router.get("/assets/{asset_id}/delete-preview", summary="删除范围预览")
def get_asset_delete_preview(asset_id: str, session: SessionDep) -> Any:
    with domain_errors():
        ensure_active(session, action="reading_insights.assets")
        return delete_preview(session, asset_id)


@router.delete("/assets/{asset_id}", summary="删除资产及选定的派生副本")
def delete_asset_route(
    asset_id: str,
    session: SessionDep,
    also_report_ids: list[str] | None = None,
) -> Any:
    with domain_errors(), write_tx(session):
        ensure_active(session, action="reading_insights.assets")
        removed = delete_asset(session, asset_id, also_report_ids=also_report_ids or ())
        return {"deleted": True, "removed_reports": removed}


@router.get("/reports/{report_id}/input-status", summary="报告有效输入是否变化")
def get_report_input_status(report_id: str, session: SessionDep) -> Any:
    from papersteward.reading.insights.reports import report_input_status

    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        return report_input_status(session, read_report(session, report_id), now=utc_now())


@router.get("/reports/{report_id}/series", summary="完整系列内分页读取版本")
def get_report_series(report_id: str, session: SessionDep, limit: int = 50, offset: int = 0) -> Any:
    from papersteward.reading.insights.reports import list_series_reports

    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        if not 1 <= limit <= 200 or offset < 0:
            raise InvalidRequestError("系列分页参数无效")
        detail = read_report(session, report_id)
        return {
            "items": list_series_reports(session, detail, limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
            "scope_known": detail.context is not None and detail.context.status == "complete",
        }


@router.get("/reports/{report_id}/compare/{other_id}", summary="校验同系列后比较事实")
def compare_reports(report_id: str, other_id: str, session: SessionDep) -> Any:
    from papersteward.reading.insights.reports import ensure_same_series

    with domain_errors():
        ensure_active(session, action="reading_insights.report")
        detail, other = read_report(session, report_id), read_report(session, other_id)
        ensure_same_series(detail, other)
        return {"base": detail.content.metrics, "other": other.content.metrics}
