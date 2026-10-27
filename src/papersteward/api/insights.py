"""阅读回顾 JSON 接口与页面共用的查询解析；统计计算在服务层。"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy.orm import Session

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.api.workspace_runtime import resolve_workspace
from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.errors import InvalidRequestError, NotAuthorizedError, NotFoundError
from papersteward.reading.insights.aggregate import (
    OVERVIEW_DETAIL_DEFAULT_LIMIT,
    OVERVIEW_DETAIL_MAX_LIMIT,
)
from papersteward.reading.insights.analysis import (
    candidates_for_paper,
    candidates_view_for_run,
    list_analysis_runs,
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
from papersteward.reading.insights.authorization import (
    active_grant_for_purpose,
    analysis_authorization_status,
    grant_analysis_authorization,
    revoke_analysis_authorization,
)
from papersteward.reading.insights.contracts import (
    SYNTHESIS_PURPOSE,
    AnalysisAuthorizationStatus,
    AnalysisGrantInput,
    AnalysisGrantView,
    AssetCreateInput,
    AssetRevisionDetail,
    AssetRevisionInput,
    AssetView,
    CandidateDecisionInput,
    ExcerptInput,
    ExcerptView,
    InsightOverview,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightsModel,
    ManifestSelectionInput,
    PeriodKind,
    ReadTimeInput,
    ReadTimeView,
    ReviewMode,
    StructuredFeedbackInput,
    StructuredFeedbackView,
    SynthesisManifestInput,
)
from papersteward.reading.insights.decisions import (
    candidate_with_decision,
    decide_candidate,
    decision_view_for_candidate,
)
from papersteward.reading.insights.materials import (
    add_excerpt,
    build_synthesis_manifest,
    consume_preview_token,
    create_manifest,
    create_preview_token,
    delete_excerpt,
    list_excerpts,
    manifest_content_of,
    store_manifest,
    validate_preview_token,
)
from papersteward.reading.insights.structured_feedback import (
    revise_structured_feedback,
    structured_feedback_view,
)
from papersteward.reading.insights.timeline import (
    read_time_view,
    record_reading_time,
    workspace_timezone,
)
from papersteward.storage.insights import (
    ReadingAnalysisGrantRepository,
    ReadingAnalysisManifestRepository,
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


@router.get(
    "/papers/{paper_id}/feedback-context",
    response_model=StructuredFeedbackView,
    summary="查看结构化阅读理由",
)
def get_feedback_context(paper_id: str, session: SessionDep) -> Any:
    with domain_errors():
        return structured_feedback_view(session, paper_id)


@router.post(
    "/papers/{paper_id}/feedback-context",
    response_model=StructuredFeedbackView,
    status_code=status.HTTP_201_CREATED,
    summary="记录、修订或清除结构化阅读理由",
)
def post_feedback_context(paper_id: str, body: StructuredFeedbackInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        return revise_structured_feedback(session, paper_id, body, now=utc_now())


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
        structured_changes=overview.structured_changes[:detail_limit],
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

from fastapi.responses import PlainTextResponse  # noqa: E402
from pydantic import Field  # noqa: E402

from papersteward.jobs.ui_flow import FlowRequest, ReviewParams  # noqa: E402
from papersteward.reading.insights.contracts import (  # noqa: E402
    ReportDetail,
)
from papersteward.reading.insights.export import ReportExportFormat, export_report  # noqa: E402
from papersteward.reading.insights.reports import (  # noqa: E402
    delete_report,
    list_reports,
    read_report,
    save_sanitized_report,
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
    with domain_errors(), write_tx(session):
        ensure_active(session, action="reading_insights.report")
        delete_report(session, report_id)
        return {"deleted": True}


@router.post(
    "/reports/{report_id}/sanitized",
    response_model=ReportDetail,
    status_code=status.HTTP_201_CREATED,
    summary="另存脱敏版本（清除资产标题与正文副本）",
)
def save_sanitized_report_route(report_id: str, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        ensure_active(session, action="reading_insights.report")
        return save_sanitized_report(session, report_id, now=utc_now())


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


@router.get("/assets/{asset_id}/delete-preview", summary="删除范围预览（含私人标题/正文副本）")
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


# —— 阅读分析（R2-02）：授权、材料清单、预览令牌与摘录 ————————————————


@router.get(
    "/analysis/authorization",
    response_model=AnalysisAuthorizationStatus,
    summary="查看阅读分析授权现状",
)
def get_analysis_authorization(session: SessionDep, purpose: str = "reading_reflection") -> Any:
    with domain_errors():
        if purpose not in {"reading_reflection", SYNTHESIS_PURPOSE}:
            raise InvalidRequestError("用途无效", details={"field": "purpose"})
        return analysis_authorization_status(session, settings=get_settings(), purpose=purpose)


@router.post(
    "/analysis/grants",
    response_model=AnalysisGrantView,
    status_code=status.HTTP_201_CREATED,
    summary="授权阅读分析用途（独立于模型连接的知情同意）",
)
def post_analysis_grant(body: AnalysisGrantInput, session: SessionDep) -> Any:
    with domain_errors(), session.begin():
        return grant_analysis_authorization(
            session,
            scope_mode=body.scope_mode,
            material_classes=list(body.material_classes),
            settings=get_settings(),
            now=utc_now(),
            purpose=body.purpose,
        )


@router.post(
    "/analysis/grants/{grant_id}/revoke",
    response_model=AnalysisGrantView,
    summary="撤回阅读分析授权；此后发送边界一律拒绝",
)
def post_analysis_revoke(grant_id: str, session: SessionDep) -> Any:
    with domain_errors(), session.begin():
        return revoke_analysis_authorization(session, grant_id=grant_id, now=utc_now())


@router.post(
    "/analysis/manifests",
    status_code=status.HTTP_201_CREATED,
    summary="按选材生成材料清单与短期预览令牌（不发送任何请求）",
)
def post_analysis_manifest(body: ManifestSelectionInput, session: SessionDep) -> dict[str, Any]:
    with domain_errors(), write_tx(session):
        row, _created = create_manifest(
            session,
            paper_ids=list(body.paper_ids),
            kinds=list(body.kinds),
            exclusions=list(body.exclusions),
            now=utc_now(),
            purpose=body.purpose,
        )
        token = create_preview_token(
            session, manifest_id=row.id, now=utc_now(), purpose=body.purpose
        )
    return {
        "manifest": manifest_content_of(row).model_dump(mode="json"),
        "preview": token.model_dump(mode="json"),
    }


@router.get(
    "/analysis/previews/{token}",
    summary="只读校验预览令牌；GET 不创建任何外部请求",
)
def get_analysis_preview(token: str, session: SessionDep) -> dict[str, Any]:
    with domain_errors():
        row = validate_preview_token(session, token=token, now=utc_now())
        return {"manifest_hash": row.manifest_hash, "purpose": row.purpose, "valid": True}


@router.post(
    "/papers/{paper_id}/excerpts",
    response_model=ExcerptView,
    status_code=status.HTTP_201_CREATED,
    summary="为归档补充一条用户摘录（私人材料）",
)
def post_paper_excerpt(paper_id: str, body: ExcerptInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        return add_excerpt(session, paper_id, data=body, now=utc_now())


@router.get(
    "/papers/{paper_id}/excerpts",
    response_model=list[ExcerptView],
    summary="读取归档的用户摘录",
)
def get_paper_excerpts(paper_id: str, session: SessionDep) -> Any:
    with domain_errors():
        return list_excerpts(session, paper_id)


@router.delete(
    "/papers/{paper_id}/excerpts/{excerpt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一条用户摘录",
)
def delete_paper_excerpt(paper_id: str, excerpt_id: str, session: SessionDep) -> Response:
    with domain_errors(), session.begin():
        delete_excerpt(session, paper_id, excerpt_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


# —— 分析提交与候选（R2-03）———————————————————————————————————————


class AnalysisSubmitInput(InsightsModel):
    """提交单篇分析：预览令牌绑定清单与模型配置版本，提交时核对并消费。"""

    preview_token: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def submit_analysis(
    request: Request, body: AnalysisSubmitInput, session: Session
) -> dict[str, str]:
    """校验预览令牌与授权 → 提交 ui.insight_analysis → 消费令牌。

    令牌在任务预约成功后消费：提交冲突（已有任务运行）时不消耗，可重试。
    """
    from papersteward.jobs.ui_flow import AnalysisRunParams, FlowRequest

    token_row = validate_preview_token(session, token=body.preview_token, now=utc_now())
    grants = ReadingAnalysisGrantRepository(session).active_for_purpose(token_row.purpose)
    grant = next((item for item in grants if item.scope_mode == "workspace"), None) or next(
        (
            item
            for item in grants
            if item.scope_mode == "once" and item.manifest_hash == token_row.manifest_hash
        ),
        None,
    )
    if grant is None:
        raise NotAuthorizedError("阅读分析尚未授权：请先核对数据去向并授权")
    manifest = ReadingAnalysisManifestRepository(session).get(token_row.manifest_id)
    if manifest is None or manifest.manifest_hash != token_row.manifest_hash:
        raise InvalidRequestError("清单与令牌不一致，请重新生成预览")
    run_id = body.run_id or uuid4().hex
    spec = FlowRequest(
        run_id=run_id,
        kind="insight_analysis",
        analysis=AnalysisRunParams(
            manifest_id=token_row.manifest_id,
            manifest_hash=token_row.manifest_hash,
            grant_id=grant.id,
            purpose=token_row.purpose,
        ),
    )
    resolve_workspace(request).ui_flows.submit(spec)
    with write_tx(session):
        consume_preview_token(session, token_row, now=utc_now())
    return {"run_id": run_id, "run_url": f"/ui/runs/{run_id}"}


@router.post(
    "/analysis/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交单篇分析任务（消费预览令牌；发送边界在运行内逐篇核对）",
)
def post_analysis_run(
    body: AnalysisSubmitInput, request: Request, session: SessionDep
) -> dict[str, Any]:
    with domain_errors():
        return submit_analysis(request, body, session)


# —— 跨文献知识版回顾（R2-05）—————————————————————————————————————


class SynthesisSubmitInput(InsightsModel):
    """提交知识版回顾：预览令牌绑定清单与模型配置版本，提交时核对并消费。"""

    preview_token: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def submit_synthesis(
    request: Request, body: SynthesisSubmitInput, session: Session
) -> dict[str, str]:
    """校验令牌与授权 → 提交 ui.insight_synthesis → 消费令牌。

    回顾范围取自清单冻结的 ``scope_request``，不能只凭界面参数改范围；
    任务预约成功后才消费令牌，提交冲突时不消耗，可重试。
    """
    from papersteward.jobs.ui_flow import FlowRequest, SynthesisRunParams

    token_row = validate_preview_token(
        session, token=body.preview_token, purpose=SYNTHESIS_PURPOSE, now=utc_now()
    )
    grant = active_grant_for_purpose(
        session, purpose=token_row.purpose, manifest_hash=token_row.manifest_hash
    )
    if grant is None:
        raise NotAuthorizedError("知识版回顾尚未授权：请先核对数据去向并授权")
    manifest = ReadingAnalysisManifestRepository(session).get(token_row.manifest_id)
    if manifest is None or manifest.manifest_hash != token_row.manifest_hash:
        raise InvalidRequestError("清单与令牌不一致，请重新生成预览")
    content = manifest_content_of(manifest)
    query = content.scope_request
    if query is None:
        raise InvalidRequestError("清单缺少回顾范围，请重新生成预览")
    period = query.period
    run_id = body.run_id or uuid4().hex
    spec = FlowRequest(
        run_id=run_id,
        kind="insight_synthesis",
        synthesis=SynthesisRunParams(
            period=period.kind.value,
            mode=query.mode.value,
            anchor_date=period.anchor_date,
            start_date=period.start_date,
            end_date=period.end_date,
            tag=query.tag,
            manifest_id=token_row.manifest_id,
            manifest_hash=token_row.manifest_hash,
            grant_id=grant.id,
            purpose=SYNTHESIS_PURPOSE,
        ),
    )
    resolve_workspace(request).ui_flows.submit(spec)
    with write_tx(session):
        consume_preview_token(session, token_row, now=utc_now())
    return {"run_id": run_id, "run_url": f"/ui/runs/{run_id}"}


@router.post(
    "/synthesis/manifests",
    status_code=status.HTTP_201_CREATED,
    summary="按回顾范围生成知识版材料清单与短期预览令牌（不发送任何请求）",
)
def post_synthesis_manifest(body: SynthesisManifestInput, session: SessionDep) -> dict[str, Any]:
    with domain_errors():
        ensure_active(session, action="reading_insights.synthesis")
        now = utc_now()
        content = build_synthesis_manifest(
            session,
            query=body.query,
            kinds=list(body.kinds),
            exclusions=list(body.exclusions),
            now=now,
        )
        with write_tx(session):
            row, _created = store_manifest(session, content, now=now)
            token = create_preview_token(
                session, manifest_id=row.id, now=now, purpose=SYNTHESIS_PURPOSE
            )
    return {
        "manifest": content.model_dump(mode="json"),
        "manifest_id": row.id,
        "preview": token.model_dump(mode="json"),
    }


@router.post(
    "/synthesis/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交知识版回顾任务（消费预览令牌；发送边界在运行内核对）",
)
def post_synthesis_run(
    body: SynthesisSubmitInput, request: Request, session: SessionDep
) -> dict[str, Any]:
    with domain_errors():
        return submit_synthesis(request, body, session)


@router.get(
    "/analysis/runs",
    summary="最近的分析运行摘要",
)
def get_analysis_runs(session: SessionDep) -> list[dict[str, Any]]:
    with domain_errors():
        return list_analysis_runs(session)


@router.get(
    "/analysis/runs/{run_id}/candidates",
    summary="某次分析运行的候选列表（未经确认，仅属主可读）",
)
def get_run_candidates(run_id: str, session: SessionDep) -> list[dict[str, Any]]:
    with domain_errors():
        return candidates_view_for_run(session, run_id)


@router.get(
    "/analysis/candidates",
    summary="按论文查询全部候选",
)
def get_paper_candidates(paper_id: str, session: SessionDep) -> list[dict[str, Any]]:
    with domain_errors():
        return candidates_for_paper(session, paper_id)


# —— 候选决定（R2-04）——————————————————————————————————————————————


@router.post(
    "/analysis/candidates/{candidate_id}/decisions",
    status_code=status.HTTP_201_CREATED,
    summary="处理候选（确认生成资产／并入已有资产／忽略）；同候选重复提交重放同一结果",
)
def post_candidate_decision(
    candidate_id: str, body: CandidateDecisionInput, session: SessionDep
) -> dict[str, Any]:
    with domain_errors(), write_tx(session):
        if body.candidate_id != candidate_id:
            raise InvalidRequestError("请求体与路径中的候选不一致")
        ensure_active(session, action="reading_insights.assets")
        outcome = decide_candidate(session, body, now=utc_now())
        return {
            "asset_id": outcome.asset_id,
            "verification": outcome.candidate.verification,
            "asset_url": (
                f"/ui/reading-insights/assets/{outcome.asset_id}" if outcome.asset_id else None
            ),
        }


@router.get(
    "/analysis/candidates/{candidate_id}/decisions",
    summary="查询候选的处理决定",
)
def get_candidate_decision(candidate_id: str, session: SessionDep) -> dict[str, Any]:
    with domain_errors():
        decision = decision_view_for_candidate(session, candidate_id)
        if decision is None:
            raise NotFoundError("该候选还没有处理决定")
        return decision


@router.get(
    "/analysis/candidates/{candidate_id}",
    summary="候选详情（含决定状态）",
)
def get_candidate_detail(candidate_id: str, session: SessionDep) -> dict[str, Any]:
    with domain_errors():
        return candidate_with_decision(session, candidate_id)
