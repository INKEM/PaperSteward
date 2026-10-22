"""本地HTML/HTMX薄页面；业务规则仅调用现有服务。"""

import json
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.core.entities import utc_now
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.delivery.actions import apply_action
from papersteward.delivery.archive import list_archives, read_archive
from papersteward.delivery.contracts import Action, DigestCard
from papersteward.delivery.render import safe_url
from papersteward.delivery.service import create_digest
from papersteward.delivery.shelf_view import (
    ShelfItem,
    ShelfPage,
    ShelfSummary,
    list_shelf,
    parse_kind,
    shelf_item_for_paper,
    shelf_summaries,
)
from papersteward.errors import AppError, NotFoundError
from papersteward.scoring.contracts import QueueItem
from papersteward.scoring.queue import _context, explain_score, read_queue
from papersteward.storage.delivery import read_digest
from papersteward.storage.repositories import PaperRepository, SummaryRepository

router = APIRouter(prefix="/ui", include_in_schema=False)
STATIC = Path(__file__).resolve().parents[1] / "static"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}


def _page(title: str, body: str) -> HTMLResponse:
    config = {
        "allowEval": False,
        "allowScriptTags": False,
        "selfRequestsOnly": True,
        "historyCacheSize": 0,
        "includeIndicatorStyles": False,
        "responseHandling": [
            {"code": "[23]..", "swap": True},
            {"code": "[45]..", "swap": True, "error": True},
        ],
    }
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="htmx-config" content="{escape(json.dumps(config))}">'
        f"<title>{escape(title)} · PaperSteward</title>"
        '<link rel="stylesheet" href="/ui/static/style.css">'
        '<script src="/ui/static/htmx.js" defer></script></head><body>'
        '<header><a class="brand" href="/ui/today">PaperSteward</a>'
        '<nav aria-label="主导航"><a href="/ui/today">今日</a>'
        '<a href="/ui/queue">阅读队列</a><a href="/ui/shelves">特殊桶</a>'
        '<a href="/ui/archive">归档</a></nav></header>'
        f"<main><h1>{escape(title)}</h1>{body}</main>"
        "<footer>阅读反馈保存在本地。备注不会发送到外部模型。</footer></body></html>",
        headers=HEADERS,
    )


def _error(exc: DomainError | AppError | ValidationError) -> HTMLResponse:
    message = (
        exc.message
        if isinstance(exc, DomainError | AppError)
        else "输入无效，请检查评分、方式或备注。"
    )
    return HTMLResponse(
        f'<article><p role="alert">{escape(message)}</p><a href="/ui/today">返回今日</a></article>',
        status_code=422 if isinstance(exc, ValidationError) else 409,
        headers=HEADERS,
    )


def _day(session: SessionDep, explicit: date | None = None) -> date:
    ctx = _context(session, utc_now())
    return explicit or utc_now().astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date()


def _form(paper_id: str, kind: str, *, mode: str = "skim", note: str | None = None) -> str:
    url = "/ui/actions/" + quote(paper_id, safe="")
    labels = {
        "done": "完成并归档",
        "score": "修订评分",
        "note": "修订备注",
        "snooze": "稍后",
        "skip": "跳过7天",
        "reject": "不相关",
    }
    fields = ""
    if kind == "done":
        fields += (
            '<label>实际阅读方式<select name="mode">'
            + "".join(
                f'<option value="{value}"{" selected" if value == mode else ""}>{label}</option>'
                for value, label in (("deep", "精读"), ("skim", "粗读"))
            )
            + "</select></label>"
        )
    if kind in {"done", "score"}:
        fields += (
            '<label>主观评分<select name="score"><option value="">不评分</option>'
            + "".join(f'<option value="{n}">{n} 分</option>' for n in range(1, 6))
            + "</select></label>"
        )
    if kind in {"done", "note", "reject"}:
        fields += f"<label>{'原因' if kind == 'reject' else '备注'}"
        fields += '<textarea name="note" maxlength="10000"'
        fields += (
            " required" if kind == "reject" else ""
        ) + f">{escape(note or '')}</textarea></label>"
    if kind == "snooze":
        fields += '<label>延后<select name="days"><option>3</option><option>7</option>'
        fields += "<option>14</option></select>天</label>"
    return (
        f'<form method="post" action="{url}" hx-post="{url}" hx-target="closest article" '
        'hx-swap="outerHTML" hx-headers=\'{"X-PaperSteward-UI":"1"}\'>'
        f'<input type="hidden" name="kind" value="{kind}">'
        f'<input type="hidden" name="request_id" value="{uuid4().hex}">'
        f'{fields}<button type="submit">{labels[kind]}</button></form>'
    )


def _card(session: SessionDep, item: QueueItem) -> str:
    paper = PaperRepository(session).get(item.paper_id)
    mode = item.read_mode.value if isinstance(item, DigestCard) else "skim"
    meta = ""
    if isinstance(item, DigestCard):
        meta = f'<p class="muted">{escape(", ".join(item.authors[:3]) or "作者未知")} · '
        meta += f"{escape(item.venue or 'venue未知')}</p>"
        meta += "<p>" + escape(" / ".join([item.primary_tag, *item.secondary_tags])) + "</p>"
        for label, raw in (("原文", item.url), ("PDF", item.pdf_url), ("代码", item.code_url)):
            if url := safe_url(raw):
                meta += f'<a class="source" rel="noreferrer" href="{escape(url)}">{label}</a> '
    body = (
        f'<article><span class="tag">{escape(item.primary_tag)}</span>'
        f"<h2>{escape(item.title)}</h2>{meta}<blockquote>{escape(item.summary_100)}</blockquote>"
        f"<p>综合 {item.priority:.1f} · 排序 {item.rank_score:.1f}</p>"
    )
    if isinstance(item, DigestCard):
        body += f"<p><strong>{'建议精读' if mode == 'deep' else '建议粗读'}</strong> · "
        body += f"{escape(item.reason)}</p>"
    body += f'<a href="/ui/papers/{quote(item.paper_id, safe="")}">详情与评分解释</a>'
    if paper and paper.status in {PaperStatus.DONE, PaperStatus.ARCHIVED}:
        body += '<p class="success">已完成归档</p>'
    elif paper and paper.status in {PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING}:
        body += (
            "<details><summary>记录阅读与反馈</summary>"
            + _form(item.paper_id, "done", mode=mode)
            + "</details>"
        )
        body += (
            "<details><summary>稍后、跳过或不相关</summary>"
            + "".join(_form(item.paper_id, k) for k in ("snooze", "skip", "reject"))
            + "</details>"
        )
    else:
        body += "<p>此条目当前已延期或不在阅读队列。</p>"
    return body + "</article>"


def _shelf_inner(item: ShelfItem) -> str:
    tier_value = item.tier.tier.value if item.tier is not None else "unrated"
    tier_label = item.tier.tier.value if item.tier is not None else "未评估"
    body = (
        f'<p><span class="tag tier-{tier_value}">{escape(tier_label)}</span> '
        f"主标签 {escape(item.primary_tag or '—')} · 状态 {escape(item.paper.status.value)}</p>"
    )
    meta = []
    if item.paper.authors:
        meta.append(", ".join(item.paper.authors[:3]))
    if item.paper.venue:
        meta.append(item.paper.venue)
    if meta:
        body += f'<p class="muted">{escape(" · ".join(meta))}</p>'
    if url := safe_url(item.paper.url):
        body += f'<p><a class="source" rel="noreferrer" href="{escape(url)}">原文</a></p>'
    if item.nomination is not None:
        body += f"<p>{escape(item.nomination.reason)}</p>"
    if item.needs_assessment:
        body += '<p class="muted">尚未评估档位，请运行 papersteward shelf assess。</p>'
    for evidence in item.evidence:
        label = "有效证据" if evidence.valid else "未核对证据"
        body += (
            f'<blockquote><span class="muted">{label}</span> {escape(evidence.quote)}</blockquote>'
        )
    for line in item.tier.explanation if item.tier is not None else ():
        body += f"<p>{escape(line)}</p>"
    if item.nomination is not None and item.nomination.review_layers:
        body += f"<p>覆盖层级：{escape(', '.join(item.nomination.review_layers))}</p>"
    if item.nomination is not None and item.nomination.classic_basis:
        body += f"<p>经典依据：{escape(', '.join(item.nomination.classic_basis))}</p>"
    if item.nomination is not None and item.nomination.bridge_memo is not None:
        body += "<details open><summary>桥接假设</summary>"
        for key, label in (
            ("source_mechanism", "源机制"),
            ("shared_structure", "共享结构"),
            ("transfer_experiment", "迁移实验"),
            ("falsification_signal", "证伪信号"),
        ):
            value = item.nomination.bridge_memo.get(key)
            if isinstance(value, str) and value.strip():
                body += f"<p><strong>{label}</strong>：{escape(value)}</p>"
        body += "</details>"
    if item.tier is not None and item.tier.note:
        body += f'<p class="muted">改档备注：{escape(item.tier.note)}</p>'
    return body


def _shelf_section(summary: ShelfSummary, page: ShelfPage, *, next_url: str | None) -> str:
    body = (
        f"<section><h2>{escape(summary.name)} "
        f'<span class="tag">{escape(summary.kind.value)}</span></h2>'
        f'<p class="muted">周上限 {summary.quota} · 共 {summary.total} 篇 · '
        f"high {summary.high} / medium {summary.medium} / low {summary.low} / "
        f"未评估 {summary.unrated}</p>"
    )
    for item in page.items:
        body += (
            f'<article><h2><a href="/ui/papers/{quote(item.paper.id, safe="")}">'
            f"{escape(item.paper.title)}</a></h2>{_shelf_inner(item)}</article>"
        )
    if not page.items:
        body += '<p class="muted">暂无内容（不占位）。</p>'
    if next_url is not None:
        body += f'<p><a href="{escape(next_url)}">查看更多</a></p>'
    return body + "</section>"


@router.get("/static/htmx.js")
def htmx() -> FileResponse:
    return FileResponse(STATIC / "htmx-2.0.11.min.js", media_type="application/javascript")


@router.get("/static/style.css")
def style() -> FileResponse:
    return FileResponse(STATIC / "style.css", media_type="text/css")


@router.get("/today")
def today(session: SessionDep, day: date | None = None) -> HTMLResponse:
    try:
        selected = _day(session, day)
        current = _day(session)
        try:
            digest = read_digest(session, selected)
        except NotFoundError:
            label = "今日" if selected == current else "该日"
            return _page(
                "今日阅读",
                f'<p class="muted">{selected} · 按阅读容量与主题平衡，安排该日文献。</p>'
                '<form action="/ui/digests" method="post" hx-post="/ui/digests" '
                'hx-target="main" hx-headers=\'{"X-PaperSteward-UI":"1"}\'>'
                f'<input type="hidden" name="day" value="{selected.isoformat()}">'
                f"<button>生成{label}日报</button></form>",
            )
        body = f'<p class="muted">{selected} · 队列 {digest.queue_total} 篇 · '
        body += f"待确认 {digest.pending_review} 篇</p>"
        body += (
            "".join(_card(session, item) for item in digest.items)
            or "<article>今日暂无合格候选，给阅读留一点空白。</article>"
        )
        body += (
            f"<p>{escape(digest.special_status)} · 延期/冷却/配额未选 {len(digest.deferred)} 篇</p>"
        )
        return _page("今日阅读", body)
    except (DomainError, AppError) as exc:
        return _page("今日阅读", f'<p role="alert">{escape(exc.message)}</p>')


@router.get("/queue")
def queue(session: SessionDep, offset: int = 0) -> HTMLResponse:
    try:
        page = read_queue(session, _day(session), now=utc_now(), limit=50, offset=offset)
        body = f"<p>待阅读 {page.total} 篇</p>" + "".join(
            _card(session, item) for item in page.items
        )
        if offset + 50 < page.total:
            body += f'<a href="/ui/queue?offset={offset + 50}">下一页</a>'
        return _page("阅读队列", body or "<p>队列为空。</p>")
    except (DomainError, AppError) as exc:
        return _page("阅读队列", f'<p role="alert">{escape(exc.message)}</p>')


@router.get("/shelves")
def shelves(session: SessionDep, kind: str | None = None, offset: int = 0) -> HTMLResponse:
    try:
        all_summaries = list(shelf_summaries(session))
        summaries = all_summaries
        if kind is not None:
            parsed = parse_kind(kind)
            summaries = [summary for summary in summaries if summary.kind is parsed]
        links = '<p><a href="/ui/shelves">全部</a>'
        for summary in all_summaries:
            links += (
                f' · <a href="/ui/shelves?kind={quote(summary.kind.value)}">'
                f"{escape(summary.name)}（{summary.total}）</a>"
            )
        body = links + "</p>"
        for summary in summaries:
            page = list_shelf(
                session,
                summary.kind,
                limit=50,
                offset=offset if kind is not None else 0,
            )
            next_url = None
            if kind is not None and offset + 50 < page.total:
                next_url = f"/ui/shelves?kind={quote(summary.kind.value)}&offset={offset + 50}"
            body += _shelf_section(summary, page, next_url=next_url)
        return _page("特殊桶", body)
    except (DomainError, AppError) as exc:
        return _page("特殊桶", f'<p role="alert">{escape(exc.message)}</p>')


@router.get("/papers/{paper_id}")
def detail(paper_id: str, session: SessionDep) -> HTMLResponse:
    try:
        paper = PaperRepository(session).get(paper_id)
        if paper is None:
            raise NotFoundError("文献不存在")
        if paper.status is PaperStatus.ARCHIVED:
            record = read_archive(session, paper_id)
            body = (
                '<p class="success">已完成归档</p><pre>'
                + escape(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2))
                + "</pre>"
            )
            body += _form(paper_id, "score") + _form(paper_id, "note", note=record.note)
            return _page(
                str(record.content.get("metadata", {}).get("title", paper_id)),
                "<article>" + body + "</article>",
            )
        shelf_item = shelf_item_for_paper(session, paper_id)
        if shelf_item is not None:
            summary = SummaryRepository(session).get(paper_id)
            body = "<article><blockquote>"
            body += escape(summary.summary_100 if summary else "概括未就绪") + "</blockquote>"
            body += _shelf_inner(shelf_item)
            if paper.status in {PaperStatus.PUSHED, PaperStatus.READING}:
                body += _form(paper_id, "done")
            return _page(paper.title, body + "</article>")
        _day(session)
        score = explain_score(session, paper_id, now=utc_now())
        summary = SummaryRepository(session).get(paper_id)
        body = "<article><blockquote>"
        body += escape(summary.summary_100 if summary else "概括未就绪") + "</blockquote>"
        body += (
            "<details open><summary>评分解释</summary><pre>"
            + escape(score.model_dump_json(indent=2))
            + "</pre></details>"
        )
        if paper.status in {PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING}:
            body += _form(paper_id, "done")
        return _page(paper.title, body + "</article>")
    except (DomainError, AppError) as exc:
        return _page("文献详情", f'<p role="alert">{escape(exc.message)}</p>')


@router.get("/archive")
def archive(
    session: SessionDep,
    query: str | None = None,
    tag: str | None = None,
    min_score: int | None = None,
    offset: int = 0,
) -> HTMLResponse:
    try:
        page = list_archives(session, query=query, tag=tag, min_score=min_score, offset=offset)
        body = (
            '<form method="get"><label>搜索<input name="query" value="'
            + escape(query or "")
            + '"></label><button>查找</button></form>'
        )
        body += '<p>导出：<a href="/archive/export?format=bibtex">BibTeX</a> · '
        body += '<a href="/archive/export?format=csv">CSV</a> · '
        body += '<a href="/archive/export?format=markdown">Markdown</a></p>'
        for record in page["items"]:
            title = record.content.get("metadata", {}).get("title", record.paper_id)
            rating = record.score if record.score is not None else "未评分"
            body += f'<article><h2><a href="/ui/papers/{quote(record.paper_id, safe="")}">'
            body += f"{escape(title)}</a></h2><p>{escape(record.primary_tag)} · 评分 {rating}</p>"
            body += f"<p>{escape(record.note or '暂无备注')}</p></article>"
        if offset + 50 < page["total"]:
            from urllib.parse import urlencode

            body += (
                '<a href="/ui/archive?'
                + escape(urlencode({"query": query or "", "offset": offset + 50}))
                + '">下一页</a>'
            )
        return _page(f"阅读归档 · {page['total']} 篇", body)
    except (DomainError, AppError) as exc:
        return _page("阅读归档", f'<p role="alert">{escape(exc.message)}</p>')


def _guard(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
    ):
        raise InvariantViolation("ui.origin", "拒绝跨站操作")
    if not origin and request.headers.get("x-papersteward-ui") != "1":
        raise InvariantViolation("ui.origin", "需要同源页面操作")


@router.post("/digests")
async def generate(request: Request, session: SessionDep) -> Response:
    try:
        _guard(request)
        raw = await request.body()
        if len(raw) > 10000:
            raise InvariantViolation("ui.body", "表单过大")
        fields = parse_qs(raw.decode(), keep_blank_values=True)
        explicit = fields.get("day", [""])[-1]
        try:
            explicit_day = date.fromisoformat(explicit) if explicit else None
        except ValueError:
            raise InvariantViolation("digest.date", "日期无效") from None
        with session.begin():
            selected = _day(session, explicit_day)
            create_digest(session, selected, now=utc_now())
            target = "/ui/today" if selected == _day(session) else f"/ui/today?day={selected}"
        return (
            Response(status_code=200, headers={"HX-Redirect": target})
            if request.headers.get("hx-request")
            else RedirectResponse(target, status_code=303)
        )
    except (DomainError, AppError) as exc:
        return _error(exc)
    except (ValueError, UnicodeError):
        return HTMLResponse(
            '<article><p role="alert">表单须为UTF-8，日期须为有效日期。</p></article>',
            status_code=422,
            headers=HEADERS,
        )


@router.post("/actions/{paper_id}")
async def action(paper_id: str, request: Request, session: SessionDep) -> HTMLResponse:
    try:
        _guard(request)
        raw = await request.body()
        if len(raw) > 50000:
            raise InvariantViolation("ui.body", "表单过大")
        fields: dict[str, Any] = {
            key: values[-1]
            for key, values in parse_qs(raw.decode(), keep_blank_values=True).items()
        }
        if "score" in fields:
            fields["score"] = int(fields["score"]) if fields["score"] else None
        if "days" in fields:
            fields["days"] = int(fields["days"])
        if "note" in fields and not fields["note"]:
            fields["note"] = None
        body = Action.model_validate(fields)
        with session.begin():
            result = apply_action(session, paper_id, body, now=utc_now())
        return HTMLResponse(
            '<article><p class="success">已保存阅读记录：' + escape(result["status"]) + "</p>"
            f'<a href="/ui/papers/{quote(paper_id, safe="")}">查看详情与反馈历史</a></article>',
            headers=HEADERS,
        )
    except (DomainError, AppError, ValidationError) as exc:
        return _error(exc)
    except (ValueError, UnicodeError):
        return HTMLResponse(
            '<article><p role="alert">评分/天数须为整数，表单须为UTF-8。</p></article>',
            status_code=422,
            headers=HEADERS,
        )
