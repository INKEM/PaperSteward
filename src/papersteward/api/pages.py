"""本地HTML/HTMX薄页面；业务规则仅调用现有服务。"""

import json
from datetime import date
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.core.entities import Taxonomy, ViewConfig, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.delivery.actions import apply_action
from papersteward.delivery.archive import list_archives, read_archive
from papersteward.delivery.contracts import Action, DigestCard
from papersteward.delivery.render import safe_url
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
from papersteward.scoring.contracts import BacklogFilters, BacklogItem, QueueItem, ScoreExplanation
from papersteward.scoring.queue import _context, explain_score, read_backlog
from papersteward.scoring.views import (
    ViewPreview,
    get_view_config,
    list_presets,
    list_view_configs,
    preview_view,
)
from papersteward.storage.delivery import read_digest
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import (
    EnrichmentRepository,
    PaperRepository,
    RunRepository,
    SummaryRepository,
    TaxonomyRepository,
)

router = APIRouter(prefix="/ui", include_in_schema=False)
STATIC = Path(__file__).resolve().parents[1] / "static"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}


def _page(title: str, body: str, *, nav: str | None = None) -> HTMLResponse:
    """页面外壳。nav 为该页在主导航中的键；缺省用 title 匹配。"""
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
    links = (
        ("/ui/queue", "阅读队列", "阅读队列"),
        ("/ui/shelves", "特殊桶", "特殊桶"),
        ("/ui/views", "排序视图", "排序视图"),
        ("/ui/archive", "归档", "归档"),
        ("/ui/setup", "首次设置", "首次设置"),
        ("/ui/settings", "设置", "设置"),
        ("/ui/help", "帮助", "使用帮助"),
    )
    active = nav or title
    navigation = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if active == key else "") + f">{label}</a>"
        for url, label, key in links
    )
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="htmx-config" content="{escape(json.dumps(config))}">'
        f"<title>{escape(title)} · PaperSteward</title>"
        '<link rel="stylesheet" href="/ui/static/style.css">'
        '<script src="/ui/static/htmx.js" defer></script>'
        '<script src="/ui/static/setup.js" defer></script></head><body>'
        '<a class="skip-link" href="#main">跳到内容</a><header>'
        '<a class="brand" href="/ui/queue"><span class="brand-mark" aria-hidden="true">P</span>'
        "PaperSteward<small>留一点时间，读重要的论文</small></a>"
        f'<nav class="desktop-nav" aria-label="主导航">{navigation}</nav>'
        '<details class="mobile-nav"><summary>导航菜单</summary>'
        f'<nav aria-label="移动导航">{navigation}</nav></details></header>'
        f'<main id="main"><div class="page-heading"><p class="eyebrow">你的研究书桌</p>'
        f"<h1>{escape(title)}</h1></div>{body}</main>"
        "<footer><span>PaperSteward · 本地阅读工作区</span>"
        '<span>阅读反馈保存在本地，备注不外发。 <a href="/ui/help">帮助与隐私</a></span>'
        "</footer></body></html>",
        headers=HEADERS,
    )


@router.get("/help")
def help_page() -> HTMLResponse:
    return _page(
        "使用帮助",
        "<article><h2>从这里开始</h2><p>第一次打开时，先在「首次设置」连接模型服务，再粘贴或上传"
        "描述研究方向的材料。Agent 会生成研究方向档案和分类标签草案；你可以用自然语言提意见，"
        "查看每版差异，满意后确认开始使用。</p>"
        "<p>日常顺序：手动挖掘 → 长期阅读队列 → 随时阅读。</p>"
        "<p>运行中可关闭页面，稍后从阅读队列查看；想中途退出点「停止任务」，已完成的数据保留。失败后检查原因，再用原参数重试。</p></article>"
        "<article><h2>模型与隐私</h2><p>模型地址、模型名和密钥在浏览器「设置」页填写，"
        "保存在本机工作区数据库。保存即表示同意把论文标题与摘要发送给该模型服务，"
        "用于分类、概括和评分；阅读备注不发送到模型，也不包含在同步请求中。</p>"
        "<p>采集论文时会访问所选文献来源；打开阅读队列不会自动联网或发邮件。"
        "密钥不会回显，也不会写入研究材料。</p><p>新工作区：停止后使用 "
        "<code>papersteward start --data-dir 新目录</code>，原数据保留。</p></article>"
        "<article><h2>怎么看运行进度</h2><p>队列顶部显示当前任务、处理篇数、当前论文和已用时；"
        "每 2 秒更新，任务结束自动刷新。运行页保留失败阶段与重试入口。"
        "合格论文会直接进入长期队列；排名是当前列表中的名次，评分是推荐参考；"
        "阅读容量只提供建议，不隐藏论文。任务运行时请等待完成后再提交。</p></article>"
        "<article><h2>常见状态</h2><p>没有候选：检查模型连接，先手动挖掘，再到阅读队列查看。"
        "队列为空：调整检索词再次挖掘。采集失败：检查来源/网络/检索词，原有数据保留。</p>"
        "<p>本地单用户工具，请使用默认 127.0.0.1 地址。"
        "关闭服务后再复制整个数据目录保存副本。</p></article>",
    )


def _error(exc: DomainError | AppError | ValidationError) -> HTMLResponse:
    message = (
        exc.message
        if isinstance(exc, DomainError | AppError)
        else "输入无效，请检查评分、方式或备注。"
    )
    return HTMLResponse(
        f'<article><p role="alert" tabindex="-1" autofocus>{escape(message)}</p>'
        '<a href="/ui/today">返回队列</a></article>',
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
        "reject": "标记为不感兴趣",
        "restore": "恢复推荐",
    }
    confirm = (
        ' data-confirm="标记后这篇论文不再出现在队列。确认标记为不感兴趣？"'
        if kind == "reject"
        else ""
    )
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
    return (
        f'<form method="post" action="{url}" hx-post="{url}" hx-target="closest article" '
        'hx-swap="outerHTML" hx-headers=\'{"X-PaperSteward-UI":"1"}\''
        f"{confirm}>"
        f'<input type="hidden" name="kind" value="{kind}">'
        f'<input type="hidden" name="request_id" value="{uuid4().hex}">'
        f'{fields}<button type="submit">{labels[kind]}</button></form>'
    )


def _paper_links(session: SessionDep, paper_id: str) -> str:
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        return ""
    enrichment = EnrichmentRepository(session).get(paper_id)
    body = f'<p class="muted">{escape(", ".join(paper.authors[:3]) or "作者未知")}'
    if paper.venue:
        body += " · " + escape(paper.venue)
    body += "</p><p>"
    for label, raw in (
        ("原文", paper.url),
        ("PDF", paper.pdf_url),
        ("代码", enrichment.signals.code_url if enrichment else None),
    ):
        if url := safe_url(raw):
            body += f'<a class="source" rel="noreferrer" href="{escape(url)}">{label}</a> '
    return body + "</p>"


def _card(
    session: SessionDep,
    item: QueueItem,
    *,
    is_new: bool = False,
    readonly: bool = False,
    taxonomy: Taxonomy | None = None,
) -> str:
    paper = PaperRepository(session).get(item.paper_id)
    mode = item.read_mode.value if isinstance(item, DigestCard) else "skim"
    rank = item.rank if isinstance(item, BacklogItem) else None
    if taxonomy is None:
        taxonomy = TaxonomyRepository(session).latest_confirmed()
    label = (
        next((tag.name for tag in taxonomy.tags if tag.code == item.primary_tag), item.primary_tag)
        if taxonomy
        else item.primary_tag
    )
    body = f'<article class="paper-card{" newly-added" if is_new else ""}">'
    if is_new:
        body += '<span class="tag new-badge">本次新增</span> '
    body += f'<span class="tag">{escape(label)}</span>'
    body += (
        f'<h2><a href="/ui/papers/{quote(item.paper_id, safe="")}">{escape(item.title)}</a></h2>'
    )
    body += _paper_links(session, item.paper_id)
    body += f"<blockquote>{escape(item.summary_100)}</blockquote>"
    body += '<p class="paper-score">'
    if rank is not None:
        body += f"<strong>第 {rank} 名</strong> · "
    body += (
        f"综合评分 {item.priority:.1f} / 100 · "
        f'<span class="muted">评分于 {item.score_date}</span></p>'
    )
    body += f'<a href="/ui/papers/{quote(item.paper_id, safe="")}">查看论文与推荐依据</a>'
    if readonly:
        return body + "</article>"
    if paper and paper.status in {PaperStatus.DONE, PaperStatus.ARCHIVED}:
        body += '<p class="success">已完成归档</p>'
    elif paper and paper.status in {PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING}:
        body += (
            "<details><summary>完成并记录阅读</summary>"
            + _form(item.paper_id, "done", mode=mode)
            + "</details>"
        )
        body += (
            "<details><summary>不感兴趣</summary>" + _form(item.paper_id, "reject") + "</details>"
        )
    return body + "</article>"


STATUS_LABELS = {
    "discovered": "已发现",
    "deduped": "已去重",
    "screened": "已筛选",
    "enriched": "已归纳",
    "summarized": "已概括",
    "classified": "已归类",
    "scored": "已评分",
    "queued": "队列中",
    "reading": "阅读中",
    "done": "已完成",
    "archived": "已归档",
    "rejected": "不感兴趣",
    "shelved": "特殊桶",
    "pushed": "已推送",
}

TIER_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _shelf_inner(item: ShelfItem) -> str:
    tier_value = item.tier.tier.value if item.tier is not None else "unrated"
    tier_label = TIER_LABELS.get(tier_value, "未评估")
    status_label = STATUS_LABELS.get(item.paper.status.value, item.paper.status.value)
    body = (
        f'<p><span class="tag tier-{tier_value}">{escape(tier_label)}</span> '
        f"主标签 {escape(item.primary_tag or '—')} · 状态 {escape(status_label)}</p>"
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
        body += (
            '<p class="muted">尚未评估档位；运行命令行 <code>papersteward shelf assess</code> '
            "后会显示高/中/低档位。</p>"
        )
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
        f"高 {summary.high} · 中 {summary.medium} · 低 {summary.low} · "
        f"未评估 {summary.unrated}</p>"
    )
    for item in page.items:
        body += (
            f'<article><h2><a href="/ui/papers/{quote(item.paper.id, safe="")}">'
            f"{escape(item.paper.title)}</a></h2>{_shelf_inner(item)}</article>"
        )
    if not page.items:
        body += '<p class="muted">该桶暂无论文。</p>'
    if next_url is not None:
        body += f'<p><a href="{escape(next_url)}">查看更多</a></p>'
    return body + "</section>"


def _view_table(preview: ViewPreview) -> str:
    headers = "".join(f"<th>{key}</th>" for key in ("R", "A", "E", "F", "H", "U"))
    rows = ""
    for item in preview.items:
        moved = (
            "—"
            if item.delta_rank == 0
            else (f"↑{item.delta_rank}" if item.delta_rank > 0 else f"↓{-item.delta_rank}")
        )
        cells = "".join(
            f"<td>{item.components[key].score:.0f}</td>" for key in ("R", "A", "E", "F", "H", "U")
        )
        rows += (
            f"<tr><td>{item.rank}</td><td>{item.default_rank}</td><td>{moved}</td>"
            f'<td><a href="/ui/papers/{quote(item.paper_id, safe="")}">'
            f"{escape(item.title)}</a></td><td>{item.view_score:.1f}</td>{cells}</tr>"
        )
    if not rows:
        return '<p class="muted">当前队列为空；先挖掘论文或刷新排序。</p>'
    return (
        "<table><thead><tr><th>#</th><th>默认</th><th>变化</th><th>标题</th>"
        f"<th>视图分</th>{headers}</tr></thead><tbody>{rows}</tbody></table>"
    )


def _view_links(preview: ViewPreview, saved: list[ViewConfig], day: date | None) -> str:
    date_part = f"&date={day.isoformat()}" if day is not None else ""
    links = "<p>预设："
    parts = []
    for preset in list_presets():
        parts.append(
            f'<a href="/ui/views?preset={quote(preset.name)}{date_part}">{escape(preset.label)}</a>'
        )
    links += " · ".join(parts) + "</p>"
    links += "<p>已保存："
    if saved:
        parts = []
        for config in saved:
            parts.append(
                f'<a href="/ui/views?name={quote(config.name)}{date_part}">'
                f"{escape(config.name)}</a>"
            )
        links += " · ".join(parts)
    else:
        links += (
            '<span class="muted">还没有保存的排序视图；可在命令行用 '
            "papersteward view save 保存自定义公式。</span>"
        )
    links += "</p>"
    formula_text = preview.view.as_json()["formula"]
    links += (
        f'<p class="muted">当前视图：{escape(preview.view.label)} · '
        f"公式：{escape(formula_text)} · 候选 {preview.candidate_total} 篇 · "
        f"{preview.changed} 篇名次变化。视图只重排当前队列，不改变全局权重与快照。</p>"
    )
    return links


@router.get("/views")
def views(
    session: SessionDep,
    preset: str | None = None,
    name: str | None = None,
    score_date: Annotated[date | None, Query(alias="date")] = None,
    limit: int = 20,
) -> HTMLResponse:
    try:
        saved = list_view_configs(session)
        view_id = None
        if name is not None:
            view_id = get_view_config(session, name).id
        preview = preview_view(
            session,
            score_date or utc_now().date(),
            preset=preset,
            view_id=view_id,
            limit=limit,
            persistent=score_date is None,
        )
        body = _view_links(preview, saved, score_date) + _view_table(preview)
        return _page("排序视图", body)
    except (DomainError, AppError) as exc:
        return _page("排序视图", f'<p role="alert">{escape(exc.message)}</p>')


def _static_headers(cache_control: str) -> dict[str, str]:
    """静态资产共用安全头；缓存策略按资产可变性区分（HTML 页面仍为 no-store）。"""
    return {**HEADERS, "Cache-Control": cache_control}


@router.get("/static/htmx.js")
def htmx() -> FileResponse:
    # 文件名内含版本号，内容永不变化：允许浏览器长缓存、不再重复下载。
    return FileResponse(
        STATIC / "htmx-2.0.11.min.js",
        media_type="application/javascript",
        headers=_static_headers("public, max-age=31536000, immutable"),
    )


@router.get("/static/setup.js")
def setup_script() -> FileResponse:
    # 自有脚本会随版本变化：no-cache 走 ETag 协商，改动能立即生效且有 304 快路径。
    return FileResponse(
        STATIC / "setup.js",
        media_type="application/javascript",
        headers=_static_headers("no-cache"),
    )


@router.get("/static/style.css")
def style() -> FileResponse:
    return FileResponse(
        STATIC / "style.css", media_type="text/css", headers=_static_headers("no-cache")
    )


def _backlog_page(
    request: Request, session: SessionDep, filters: BacklogFilters, offset: int
) -> Response:
    if get_onboarding(session).stage.value != "active":
        return RedirectResponse("/ui/setup", status_code=303)
    try:
        from papersteward.api.workflow_pages import dashboard

        now = utc_now()
        page = read_backlog(session, now=now, limit=50, offset=offset, filters=filters)
        body = dashboard(
            session, request.app.state.settings, _day(session), backlog_total=page.total
        )
        if "cancelled" in request.query_params:
            body += '<p class="notice success">任务已停止，已完成的数据保留。</p>'
        taxonomy = TaxonomyRepository(session).latest_confirmed()
        body += (
            '<form action="/ui/queue" class="queue-filters"><label>研究标签'
            '<select name="tag"><option value="">全部标签</option>'
        )
        for tag in taxonomy.tags if taxonomy else ():
            if tag.level.value == "primary" and tag.active:
                selected = " selected" if filters.tag == tag.code else ""
                body += f'<option value="{escape(tag.code)}"{selected}>{escape(tag.name)}</option>'
        body += '</select></label><label>阅读状态<select name="read_status">'
        for value, label in (("unread", "未读"), ("read", "已读"), ("all", "全部")):
            selected = " selected" if filters.read_status == value else ""
            body += f'<option value="{value}"{selected}>{label}</option>'
        body += "</select></label>"
        for name, number, label in (
            ("min_priority", filters.min_priority, "最低分"),
            ("max_priority", filters.max_priority, "最高分"),
        ):
            body += (
                f'<label>{label}<input type="number" name="{name}" value="{number:g}" '
                'min="0" max="100" step="0.1"></label>'
            )
        body += "<button>应用筛选</button></form>"
        latest = RunRepository(session).latest("ui.sync", status=RunStatus.SUCCEEDED)
        added = set(latest.stats_json.get("added_ids", [])) if latest else set()
        if page.items:
            dates = sorted({item.score_date for item in page.items})
            date_label = str(dates[0]) if len(dates) == 1 else f"{dates[0]} 至 {dates[-1]}"
            body += (
                f'<p class="muted">共 {page.total} 篇 · 排序基于 {date_label} 的评分。'
                "可随时刷新排序。</p>"
            )
            body += "".join(
                _card(session, item, is_new=item.paper_id in added, taxonomy=taxonomy)
                for item in page.items
            )
        else:
            filtered = bool(
                filters.tag
                or filters.read_status != "unread"
                or filters.min_priority > 0
                or filters.max_priority < 100
            )
            if filtered:
                body += (
                    '<article class="empty"><h2>没有论文符合当前筛选</h2>'
                    "<p>可调整或清除上方筛选条件；也可以「挖掘新论文」补充新论文。</p></article>"
                )
            else:
                body += (
                    '<article class="empty"><h2>留一点时间，读重要的论文</h2>'
                    "<p>队列还是空的。点击上方「挖掘新论文」：采集、处理、评分完成后，"
                    "论文会直接出现在这里。</p></article>"
                )
        if offset or offset + 50 < page.total:
            from urllib.parse import urlencode

            for target, label in ((max(0, offset - 50), "上一页"), (offset + 50, "下一页")):
                if (label == "上一页" and offset) or (label == "下一页" and target < page.total):
                    query = urlencode({**filters.model_dump(exclude_none=True), "offset": target})
                    body += (
                        f'<a class="button secondary" href="/ui/queue?{escape(query)}">{label}</a> '
                    )
        return _page("阅读队列", body)
    except (DomainError, AppError) as exc:
        return _page("阅读队列", f'<p role="alert">{escape(exc.message)}</p>')


@router.get("/today")
def today(request: Request, session: SessionDep, day: date | None = None) -> Response:
    if day is not None:
        return RedirectResponse(f"/ui/digests/{day}", status_code=303)
    return _backlog_page(request, session, BacklogFilters(), 0)


@router.get("/queue")
def queue(
    request: Request,
    session: SessionDep,
    tag: str | None = None,
    min_priority: Annotated[float, Query(ge=0, le=100)] = 0,
    max_priority: Annotated[float, Query(ge=0, le=100)] = 100,
    read_status: Literal["unread", "read", "all"] = "unread",
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    if min_priority > max_priority:
        return _page(
            "阅读队列",
            '<p role="alert">最低分不能高于最高分。请调整分数范围后重新筛选。</p>',
        )
    return _backlog_page(
        request,
        session,
        BacklogFilters(
            tag=tag or None,
            min_priority=min_priority,
            max_priority=max_priority,
            read_status=read_status,
        ),
        offset,
    )


@router.get("/digests/{day}")
def historical_digest(day: date, session: SessionDep) -> HTMLResponse:
    try:
        digest = read_digest(session, day)
    except NotFoundError:
        return _page(
            "历史日报",
            f'<p role="alert">{day} 没有历史日报。</p>'
            "<p>现在的论文会直接进入长期阅读队列，不再按天生成日报；"
            '已有数据保留只读。<a href="/ui/queue">返回阅读队列</a></p>',
        )
    body = f"<p>历史日报 · {day} · 只读保留</p>"
    taxonomy = TaxonomyRepository(session).latest_confirmed()
    body += "".join(_card(session, item, readonly=True, taxonomy=taxonomy) for item in digest.items)
    return _page("历史日报", body)


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
            from papersteward.api.score_render import score_explanation

            summary_data = record.content.get("summary", {})
            body = '<p class="success">已完成归档</p>'
            body += _paper_links(session, paper_id)
            body += (
                "<blockquote>"
                + escape(str(summary_data.get("summary_100", "历史概括不可用")))
                + "</blockquote>"
            )
            score_data = record.content.get("score")
            if isinstance(score_data, dict):
                body += score_explanation(ScoreExplanation.model_validate(score_data))
            rating_label = record.score if record.score is not None else "未评分"
            body += (
                f"<h2>我的阅读反馈</h2><p>评分：{rating_label} · "
                f"完成于 {record.archived_at.date()}</p>"
            )
            body += "<p>" + escape(record.note or "暂无备注") + "</p>"
            body += _form(paper_id, "score") + _form(paper_id, "note", note=record.note)
            if record.revision_history:
                body += "<details><summary>反馈修订记录</summary><ul>"
                for revision in record.revision_history:
                    body += (
                        "<li>"
                        + escape(str(revision.get("created_at", "")))
                        + " · "
                        + escape(
                            str(
                                revision.get("score")
                                if revision.get("score") is not None
                                else "未评分"
                            )
                        )
                        + " · "
                        + escape(str(revision.get("note") or ""))
                        + "</li>"
                    )
                body += "</ul></details>"
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
        if paper.status is PaperStatus.REJECTED:
            summary = SummaryRepository(session).get(paper_id)
            body = "<article><blockquote>"
            body += escape(summary.summary_100 if summary else "概括未就绪") + "</blockquote>"
            body += _paper_links(session, paper_id)
            body += (
                '<p class="notice">这篇论文已被标记为不感兴趣，不在队列中。'
                "恢复后会重新参与评分并排队。</p>"
            )
            body += _form(paper_id, "restore")
            return _page(paper.title, body + "</article>")
        _day(session)
        score = explain_score(session, paper_id, now=utc_now())
        summary = SummaryRepository(session).get(paper_id)
        body = "<article><blockquote>"
        body += escape(summary.summary_100 if summary else "概括未就绪") + "</blockquote>"
        from papersteward.api.score_render import score_explanation

        body += _paper_links(session, paper_id)
        body += score_explanation(score)
        if paper.status in {PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING}:
            body += (
                "<details><summary>完成并记录阅读</summary>"
                + _form(paper_id, "done")
                + "</details>"
            )
            body += (
                "<details><summary>不感兴趣</summary>" + _form(paper_id, "reject") + "</details>"
            )
        return _page(paper.title, body + "</article>")
    except (DomainError, AppError) as exc:
        page = _page("文献详情", f'<p role="alert">{escape(exc.message)}</p>')
        if isinstance(exc, NotFoundError):
            page.status_code = 404
        return page


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
            + '"></label><button>在归档中搜索</button></form>'
        )
        body += '<p>导出：<a href="/archive/export?format=bibtex">BibTeX</a> · '
        body += '<a href="/archive/export?format=csv">CSV</a> · '
        body += '<a href="/archive/export?format=markdown">Markdown</a></p>'
        if not page["items"]:
            if query:
                body += (
                    f'<p class="muted">没有匹配「{escape(query)}」的归档记录。'
                    "可更换关键词，或清除搜索查看全部。</p>"
                )
            else:
                body += (
                    '<p class="muted">归档还没有内容。在队列中读完论文并点「完成并归档」后，'
                    "会出现在这里。</p>"
                )
        for record in page["items"]:
            title = record.content.get("metadata", {}).get("title", record.paper_id)
            rating = record.score if record.score is not None else "未评分"
            body += f'<article><h2><a href="/ui/papers/{quote(record.paper_id, safe="")}">'
            body += f"{escape(title)}</a></h2><p>{escape(record.primary_tag)} · 评分 {rating}</p>"
            body += f"<p>{escape(record.note or '暂无备注')}</p></article>"
        if offset + 50 < page["total"]:
            from urllib.parse import urlencode

            params: dict[str, str | int] = {"query": query or "", "offset": offset + 50}
            if tag:
                params["tag"] = tag
            if min_score is not None:
                params["min_score"] = min_score
            body += f'<a href="/ui/archive?{escape(urlencode(params))}">下一页</a>'
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


@router.post("/actions/{paper_id}")
async def action(paper_id: str, request: Request, session: SessionDep) -> HTMLResponse:
    try:
        # form_utils.fields 与本模块互相引用（Q-02 待解环），此处函数内导入。
        from papersteward.api.form_utils import fields as form_fields

        form = await form_fields(request)
        payload: dict[str, Any] = dict(form)
        if "score" in payload:
            payload["score"] = int(payload["score"]) if payload["score"] else None
        if "note" in payload and not payload["note"]:
            payload["note"] = None
        body = Action.model_validate(payload)
        with session.begin():
            apply_action(session, paper_id, body, now=utc_now())
        messages = {
            "done": "已归档这篇论文：阅读记录已保存，之后可在「归档」页找到它。",
            "reject": "已标记不感兴趣，这篇论文不会再出现在队列里。",
            "restore": "已恢复推荐，下次「挖掘新论文」处理时会重新进入队列。",
            "score": "已保存新评分，之前的评分保留在修订记录里。",
            "note": "已保存新备注，之前的备注保留在修订记录里。",
        }
        headers = {**HEADERS}
        if body.kind == "reject":
            # 不整页刷新：卡片原地替换为确认与「恢复推荐」入口，误点可一键撤销。
            fragment = (
                f'<article><p class="success" tabindex="-1" autofocus>'
                f"{escape(messages['reject'])}</p>"
                + _form(paper_id, "restore")
                + f'<a href="/ui/papers/{quote(paper_id, safe="")}">查看详情</a></article>'
            )
            return HTMLResponse(fragment, headers=headers)
        if request.headers.get("hx-request") and body.kind in {"done", "score", "note"}:
            headers["HX-Refresh"] = "true"
        return HTMLResponse(
            f'<article><p class="success" tabindex="-1" autofocus>{escape(messages[body.kind])}</p>'
            f'<a href="/ui/papers/{quote(paper_id, safe="")}">查看详情与反馈历史</a></article>',
            headers=headers,
        )
    except (DomainError, AppError, ValidationError) as exc:
        return _error(exc)
    except (ValueError, UnicodeError):
        return HTMLResponse(
            '<article><p role="alert">评分须为整数，表单须为UTF-8。</p></article>',
            status_code=422,
            headers=HEADERS,
        )
