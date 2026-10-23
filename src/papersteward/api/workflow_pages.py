"""Today actions, durable run progress, and human review forms."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from html import escape
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select

from papersteward.api import review as api
from papersteward.api.deps import SessionDep
from papersteward.api.pages import HEADERS, _page
from papersteward.api.setup_pages import failure, fields, hidden, input_field
from papersteward.config import Settings
from papersteward.core.entities import Run, utc_now
from papersteward.core.enums import RunStatus, SpecialBucketKind
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, NotFoundError
from papersteward.jobs.ui_flow import FlowRequest, processing_problem
from papersteward.pipeline.context import bound_context
from papersteward.pipeline.review import list_open_reviews
from papersteward.pipeline.shelf import (
    accept_nomination,
    list_pending_nominations,
    reject_nomination,
)
from papersteward.scoring.queue import read_queue
from papersteward.storage.models import PaperRow
from papersteward.storage.repositories import RunRepository, TaxonomyRepository

router = APIRouter(prefix="/ui", include_in_schema=False)
LABELS = {
    "queued": "等待运行",
    "collecting": "采集",
    "processing": "处理",
    "digest": "生成日报",
    "completed": "已完成",
    "running": "运行中",
    "succeeded": "成功",
    "failed": "失败",
    "ui.sync": "同步 / 处理",
    "ui.digest": "生成日报",
}


def _run_form(kind: str, body: str, label: str) -> str:
    return (
        '<form method="post" action="/ui/runs">'
        + hidden("run_id", uuid4().hex)
        + hidden("kind", kind)
        + body
        + f"<button>{escape(label)}</button></form>"
    )


def dashboard(session: SessionDep, settings: Settings, day: date) -> str:
    profile, binding = bound_context(
        session, utc_now(), action="ui.dashboard", enforce_schedule=False, day=day
    )
    _, pending = list_open_reviews(session, limit=1)
    _, special_pending = list_pending_nominations(session, limit=1)
    screened = (
        session.scalar(
            select(func.count()).select_from(PaperRow).where(PaperRow.status == "screened")
        )
        or 0
    )
    qualified = read_queue(session, day, now=utc_now(), limit=1).total
    body = (
        f'<section class="dashboard"><p>已初始化 · 合格队列 {qualified} · '
        f"待处理 {screened} · 待审核 {pending + special_pending}</p>"
    )
    body += '<a class="button secondary" href="/ui/review">处理审核</a>'
    if problem := processing_problem(session, settings, binding):
        body += f'<p class="notice">{escape(problem)}</p>'
    else:
        default_query = next(
            (
                str(keyword)
                for direction in profile.parsed_json.get("subdirections", [])
                for keyword in direction.get("keywords", [])
                if str(keyword).strip()
            ),
            "retrieval",
        )
        controls = '<details><summary>采集参数</summary><label>来源<select name="source">'
        controls += "".join(
            f'<option value="{value}">{label}</option>'
            for value, label in (
                ("arxiv", "arXiv"),
                ("openalex", "OpenAlex"),
                ("semantic_scholar", "Semantic Scholar"),
            )
        )
        controls += "</select></label>" + input_field("query", default_query, "检索词（字面短语）")
        controls += (
            '<label>每次上限<input type="number" name="limit" value="10" '
            'min="1" max="50"></label></details>'
        )
        controls += (
            '<label class="check"><input type="checkbox" name="allow_external" '
            'value="yes" required>允许本次访问来源，'
            "并将论文元数据/摘要发给配置的模型</label>"
        )
        body += _run_form("sync", controls, "同步 / 处理")
    body += _run_form("digest", "", "生成今日日报")
    body += "<p>候选不足时：先同步/处理，再解决待审核项，最后生成日报。示例试运行只验证流程。</p>"
    recent = [
        run for run in RunRepository(session).list(limit=50) if run.run_type.startswith("ui.")
    ][:3]
    for run in recent:
        body += (
            f'<p>上次运行：<a href="/ui/runs/{quote(run.id, safe="")}">'
            f"{escape(LABELS.get(run.run_type, run.run_type))} · "
            f"{escape(LABELS.get(run.status.value, run.status.value))}</a></p>"
        )
    return body + "</section>"


def run_body(run: Run) -> str:
    identity = quote(run.id, safe="")
    active = run.status is RunStatus.RUNNING
    poll = (
        f' hx-get="/ui/runs/{identity}?fragment=true" hx-trigger="every 1s" hx-swap="outerHTML"'
        if active
        else ""
    )
    stage = str(run.stats_json.get("stage", "queued"))
    body = (
        f'<section id="run-status"{poll}><p role="status">'
        f"{escape(LABELS.get(stage, stage))} · "
        f"{escape(LABELS.get(run.status.value, run.status.value))}</p>"
    )
    body += (
        "<p>已完成："
        + escape(
            " / ".join(LABELS.get(step, step) for step in run.stats_json.get("completed", []))
            or "暂无"
        )
        + "</p>"
    )
    if "processed" in run.stats_json:
        body += (
            f"<p>文献处理 {run.stats_json['processed']} / {run.stats_json['processing_total']}</p>"
        )
    if run.errors_json:
        messages = [
            str(error.get("message", "步骤失败，请检查运行配置后重试。"))
            for error in run.errors_json.get("errors", [])
            if isinstance(error, dict)
        ]
        body += (
            '<p role="alert">' + escape("；".join(messages) or "步骤失败，请检查后重试。") + "</p>"
        )
    if run.status is RunStatus.FAILED and "request" in run.stats_json:
        payload = dict(run.stats_json["request"])
        kind = payload.pop("kind")
        controls = "".join(
            hidden(key, "yes" if value is True else value) for key, value in payload.items()
        )
        body += _run_form(
            kind, controls, "显式重试" + ("（访问来源与模型）" if kind == "sync" else "生成日报")
        )
    body += (
        f'<p><a href="/ui/runs/{identity}">刷新进度</a> · '
        '<a href="/ui/today">返回今日</a> · '
        '<a href="/ui/review">处理审核</a></p>'
    )
    return body + "</section>"


@router.post("/runs")
async def submit_run(request: Request) -> Response:
    try:
        values = await fields(request)
        spec = FlowRequest(
            run_id=values.get("run_id", ""),
            kind=values.get("kind"),
            source=values.get("source", "arxiv"),
            query=values.get("query", "retrieval augmented generation"),
            limit=values.get("limit", "10"),
            allow_external=values.get("allow_external") == "yes",
        )
        identity = request.app.state.ui_flows.submit(spec)
        return RedirectResponse("/ui/runs/" + identity, status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc, back="/ui/today")


@router.get("/runs/{identity}")
def run_progress(identity: str, session: SessionDep, fragment: bool = False) -> HTMLResponse:
    run = RunRepository(session).get(identity)
    if run is None:
        raise NotFoundError("运行记录不存在")
    body = run_body(run)
    return HTMLResponse(body, headers=HEADERS) if fragment else _page("运行进度", body)


@router.get("/review")
def review_page(session: SessionDep) -> HTMLResponse:
    try:
        _, binding = bound_context(session, utc_now(), action="review", enforce_schedule=False)
        taxonomy = TaxonomyRepository(session).get(
            binding["taxonomy_id"], binding["taxonomy_version"]
        )
        assert taxonomy is not None
        pairs, total = list_open_reviews(session, limit=200)
        nominations, special_total = list_pending_nominations(session, limit=200)
        body = f"<p>文献审核 {total} · 特殊桶提名 {special_total}</p>"
        if not pairs and not nominations:
            body += (
                '<article>当前没有待审核项。<a href="/ui/today">'
                "返回今日同步或生成日报</a></article>"
            )
        for item, paper in pairs:
            body += (
                "<article><h2>"
                + escape(paper.title)
                + "</h2><p>"
                + escape(" / ".join(item.reasons))
                + "</p>"
            )
            body += (
                "<details><summary>审核依据</summary><pre>"
                + escape(json.dumps(dict(item.payload_json), ensure_ascii=False, indent=2))
                + "</pre></details>"
            )
            common = (
                hidden("paper_id", paper.id)
                + input_field("reviewed_by", "local-user", "审核人")
                + '<label>审核备注<textarea name="note"></textarea></label>'
            )
            body += (
                '<form method="post" action="/ui/review/approve">'
                + common
                + "<button>批准现有分类</button></form>"
            )
            choices = "".join(
                f'<option value="{escape(tag.code)}">{escape(tag.code + " · " + tag.name)}</option>'
                for tag in taxonomy.tags
                if tag.level.value == "primary" and tag.active
            )
            body += (
                "<details><summary>修改标签后批准</summary>"
                '<form method="post" action="/ui/review/relabel">' + common
            )
            body += (
                '<label>主标签<select name="primary_tag">'
                + choices
                + "</select></label><label>副标签（逗号分隔，最多5个）"
                '<input name="secondary_tags"></label><button>修改标签并批准</button>'
                "</form></details></article>"
            )
        for nomination, paper in nominations:
            body += (
                "<article><h2>"
                + escape(paper.title)
                + "</h2><p>"
                + escape(nomination.kind.value + " · " + nomination.reason)
                + "</p>"
            )
            body += (
                "<details><summary>提名证据与桥接说明</summary><pre>"
                + escape(json.dumps(asdict(nomination), ensure_ascii=False, indent=2, default=str))
                + "</pre></details>"
            )
            common = (
                hidden("paper_id", paper.id)
                + hidden("kind", nomination.kind.value)
                + input_field("reviewed_by", "local-user", "审核人")
            )
            common += '<label>理由<textarea name="note"></textarea></label>'
            body += (
                '<form method="post" action="/ui/review/special-accept">'
                + common
                + '<label><input type="checkbox" name="force" value="yes">'
                "证据存在问题时，我明确覆盖并提供理由</label>"
                "<button>确认归入此特殊桶</button></form>"
            )
            body += (
                '<form method="post" action="/ui/review/special-reject">'
                + common
                + '<button class="secondary">保留主题归属，拒绝此提名</button></form></article>'
            )
        return _page("人工审核", body)
    except (DomainError, AppError) as exc:
        return failure(exc, back="/ui/today")


@router.post("/review/{action}")
async def review_action(action: str, request: Request, session: SessionDep) -> Response:
    try:
        values = await fields(request)
        identity = values.get("paper_id", "")
        arguments = {
            "reviewed_by": values.get("reviewed_by", ""),
            "note": values.get("note") or None,
        }
        if action == "approve":
            api.approve(identity, api.ReviewAction(**arguments), session)
        elif action == "relabel":
            api.relabel(
                identity,
                api.ReviewRelabel(
                    primary_tag=values.get("primary_tag", ""),
                    secondary_tags=[
                        tag.strip()
                        for tag in values.get("secondary_tags", "").split(",")
                        if tag.strip()
                    ],
                    **arguments,
                ),
                session,
            )
        elif action in {"special-accept", "special-reject"}:
            kind = SpecialBucketKind(values.get("kind", ""))
            with api._review_errors(), session.begin():
                if action == "special-accept":
                    accept_nomination(
                        session,
                        identity,
                        kind=kind,
                        force=values.get("force") == "yes",
                        reviewed_by=values.get("reviewed_by", ""),
                        note=values.get("note") or None,
                    )
                else:
                    reject_nomination(
                        session,
                        identity,
                        kind=kind,
                        reviewed_by=values.get("reviewed_by", ""),
                        note=values.get("note") or None,
                    )
        else:
            raise NotFoundError("未知审核操作")
        return RedirectResponse("/ui/review", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc, back="/ui/review")
