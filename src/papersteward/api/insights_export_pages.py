"""阅读回顾统一导出选择；下载私人原文前明确说明范围。"""

import json
from html import escape
from typing import Literal

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, Response

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import failure, hidden
from papersteward.api.html import HTML_HEADERS, select_options
from papersteward.core.entities import utc_now
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, InvalidRequestError
from papersteward.reading.insights.backup import personal_copy
from papersteward.reading.insights.contracts import (
    MATERIAL_KIND_LABELS,
    SYNTHESIS_REPORT_CONTRACT,
    ReportDetail,
)
from papersteward.reading.insights.export import export_report, private_content_summary
from papersteward.reading.insights.reports import prepare_report, read_report
from papersteward.storage.onboarding import get_onboarding

router = APIRouter(prefix="/ui/reading-insights/export", include_in_schema=False)


def _summary_int(summary: dict[str, object], key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) else 0


@router.get("")
def export_page(session: SessionDep, report_id: str = "") -> Response:
    from papersteward.api.insights_pages import _page

    if get_onboarding(session).stage.value != "active":
        return RedirectResponse("/ui/setup", status_code=303)
    detail = None
    if report_id:
        try:
            detail = read_report(session, report_id)
        except (DomainError, AppError) as exc:
            return failure(exc, back="/ui/reading-insights/reports")
    private_note = ""
    if detail is not None:
        summary = private_content_summary(detail.content)
        private_items = _summary_int(summary, "private_items")
        asset_titles = _summary_int(summary, "asset_titles")
        asset_bodies = _summary_int(summary, "asset_bodies")
        raw_kinds = summary.get("material_kinds")
        kinds = [
            MATERIAL_KIND_LABELS.get(str(kind), str(kind))
            for kind in (raw_kinds if isinstance(raw_kinds, list) else [])
        ]
        if detail.contract == SYNTHESIS_REPORT_CONTRACT:
            private_note += (
                "这份知识版报告有 "
                f"{private_items} 条归纳依赖私人材料；分享副本会按来源链整条排除，"
                "个人副本包含这些归纳。"
            )
            if kinds:
                private_note += "涉及的私人材料类别：" + "、".join(kinds) + "。"
        if asset_titles or asset_bodies:
            private_note += (
                f"报告另含资产标题 {asset_titles} 处、资产正文 {asset_bodies} 处；"
                "分享副本不包含这些私人原文。"
            )
    body = (
        "<h2>导出副本</h2><p>事实分享副本默认不含私人备注、资产标题和正文。"
        "报告副本只使用保存快照；全部历史副本读取当前完整事实，不受页面分页限制。</p>"
        + (f'<p class="notice">{escape(private_note)}</p>' if private_note else "")
        + '<form method="get" action="/ui/reading-insights/export/download" class="stacked-form">'
        + hidden("report_id", report_id)
        + '<label>导出范围<select name="scope">'
        + select_options(
            [("facts", "事实分享副本（默认）"), ("personal", "个人完整副本")], selected="facts"
        )
        + '</select></label><label>文件格式<select name="format">'
        + select_options(
            [("json", "JSON"), ("markdown", "Markdown（事实或报告）")], selected="json"
        )
        + '</select></label><p class="notice">个人完整副本包含私人原文：'
        + (
            "报告中已保存的资产正文与私人知识归纳；不会拼接最新资产修订。"
            if report_id
            else "全部阅读反馈备注、阅读日期修订、资产正文、适用说明及各次修订和来源；仅支持 JSON。"
        )
        + '</p><label class="check"><input type="checkbox" name="confirm_private" value="yes">'
        "我已了解并选择下载包含私人原文的个人副本（仅个人副本需要）</label>"
        '<button type="submit">下载副本</button></form>'
        "<p>文件保存在浏览器下载目录；重置工作区不会清除下载副本或本地备份。</p>"
    )
    return _page("导出阅读回顾", body)


@router.get("/download")
def download_copy(
    session: SessionDep,
    report_id: str = "",
    scope: Literal["facts", "personal"] = "facts",
    format: Literal["json", "markdown"] = "json",
    confirm_private: str = "",
) -> Response:
    from papersteward.jobs.ui_flow import ReviewParams

    if get_onboarding(session).stage.value != "active":
        return RedirectResponse("/ui/setup", status_code=303)
    try:
        if scope == "personal" and confirm_private != "yes":
            raise InvalidRequestError("个人副本包含私人原文，请返回导出页勾选说明后下载")
        now = utc_now()
        if scope == "personal" and not report_id:
            if format != "json":
                raise InvalidRequestError("含全部资产修订的个人完整副本仅支持 JSON，请返回修改格式")
            content = json.dumps(
                jsonable_encoder(personal_copy(session, now=now)), ensure_ascii=False, indent=2
            )
        else:
            if report_id:
                detail = read_report(session, report_id)
            else:
                prepared = prepare_report(session, ReviewParams(period="all"), collected_at=now)
                detail = ReportDetail(
                    report_id="live-all-history",
                    report_kind="all",
                    period_label="all-history",
                    timezone=prepared.period.timezone,
                    mode=prepared.query.mode,
                    knowledge_cutoff=prepared.knowledge_cutoff,
                    generated_at=now,
                    contract=prepared.content.contract,
                    run_id="",
                    content=prepared.content,
                )
            content = export_report(detail, format, include_private=scope == "personal")
        extension = "json" if format == "json" else "md"
        return Response(
            content,
            media_type="application/json" if format == "json" else "text/markdown",
            headers={
                **HTML_HEADERS,
                "Content-Disposition": (
                    f'attachment; filename="reading-insights-{scope}.{extension}"'
                ),
            },
        )
    except (DomainError, AppError) as exc:
        return failure(
            exc,
            back="/ui/reading-insights/export" + (f"?report_id={report_id}" if report_id else ""),
        )
