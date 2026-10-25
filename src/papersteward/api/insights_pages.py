"""阅读回顾的浏览器表单；详情页阅读时间块、补记提交与事实概览页共用此模块。

写操作沿用全站规范：``form_fields`` 做来源检查，成功/失败都重渲染
详情正文片段（htmx 原地替换），失败保留用户已填输入（X-01）。
概览页为只读实时视图，明确标注「尚未保存」，GET 无任何写入。
"""

from html import escape
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import fields as form_fields
from papersteward.api.html import HTML_HEADERS as HEADERS
from papersteward.api.html import page as _page
from papersteward.api.html import pagination as _pagination
from papersteward.api.html import select_options
from papersteward.core.entities import utc_now
from papersteward.core.enums import ReadingTimeBasis, TimePrecision
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.reading.insights.contracts import (
    CountEntry,
    InsightMetrics,
    InsightOverview,
    PaperProjection,
    ReadingTime,
    ReadTimeInput,
    ReadTimeRevision,
    ReadTimeView,
    ReviewMode,
)
from papersteward.reading.insights.timeline import record_reading_time
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import PaperRepository, TaxonomyRepository

router = APIRouter(prefix="/ui/reading-insights", include_in_schema=False)

_PRECISION_OPTIONS = (
    ("day", "按具体日期"),
    ("month", "只记得月份"),
    ("unknown", "未知（清除已记录的日期）"),
)


def reading_time_label(view: ReadTimeView) -> str:
    """阅读时间的展示标签；系统回退值注明来源，未声明即如实显示未记录。"""
    return _reading_time_text(view.effective)


def _reading_time_text(reading: ReadingTime) -> str:
    """阅读时间的展示文案；系统回退值注明来源，未知即如实显示。"""
    if reading.precision is TimePrecision.DAY and reading.day is not None:
        label = reading.day.isoformat()
        return (
            f"{label}（系统记录）" if reading.basis is ReadingTimeBasis.SYSTEM_RECORDED else label
        )
    if reading.precision is TimePrecision.MONTH and reading.month is not None:
        return f"{reading.month}（仅知月份）"
    return "未记录"


def _revision_label(item: ReadTimeRevision) -> str:
    if item.precision is TimePrecision.DAY and item.day is not None:
        return f"日 {item.day.isoformat()}"
    if item.precision is TimePrecision.MONTH and item.month is not None:
        return f"月 {item.month}"
    return "未知（清除）"


def read_time_block(view: ReadTimeView, *, draft: dict[str, str] | None = None) -> str:
    """详情页的阅读时间块：当前值、补记表单与修订历史。"""
    draft = draft or {}
    url = f"/ui/reading-insights/papers/{view.paper_id}/read-time"
    form = (
        f'<form method="post" action="{url}" class="stacked-form" '
        f'hx-post="{url}" hx-target="closest article" hx-swap="outerHTML" '
        'hx-headers=\'{"X-PaperSteward-UI":"1"}\'>'
        f'<input type="hidden" name="expected_revision" value="{view.current_revision}">'
        f'<input type="hidden" name="idempotency_key" value="{uuid4().hex}">'
        "<label>阅读时间精度"
        '<select name="precision">'
        + select_options(_PRECISION_OPTIONS, selected=draft.get("precision") or None)
        + "</select></label>"
        + '<label>实际阅读日期<input type="date" name="day" value="'
        + escape(draft.get("day", ""))
        + '"></label>'
        + '<label>实际阅读月份<input type="month" name="month" value="'
        + escape(draft.get("month", ""))
        + '"></label>'
        + '<button type="submit">记录阅读时间</button></form>'
    )
    history = ""
    if view.revisions:
        history = "<details><summary>阅读时间修订记录</summary><ul>"
        for item in view.revisions:
            history += (
                f"<li>{escape(item.recorded_at.isoformat())} · {escape(_revision_label(item))}</li>"
            )
        history += "</ul></details>"
    return (
        "<h2>阅读时间</h2>"
        f"<p>当前：{escape(reading_time_label(view))}</p>"
        "<p>只记得月份时选择「只记得月份」；补充或更正不会改变归档与评分。</p>" + form + history
    )


def _parse_form(form: dict[str, str]) -> ReadTimeInput:
    try:
        expected = int(form.get("expected_revision") or "0")
    except ValueError as exc:
        raise ValidationError("expected_revision 无效") from exc
    payload: dict[str, object] = {
        "precision": form.get("precision", ""),
        "expected_revision": expected,
        "idempotency_key": form.get("idempotency_key") or uuid4().hex,
    }
    precision = form.get("precision", "")
    if precision == TimePrecision.DAY.value:
        payload["day"] = form.get("day") or None
    if precision == TimePrecision.MONTH.value:
        payload["month"] = form.get("month") or None
    return ReadTimeInput.model_validate(payload)


@router.post("/papers/{paper_id}/read-time")
async def record_read_time(paper_id: str, request: Request, session: SessionDep) -> HTMLResponse:
    from papersteward.api.pages import _detail_article

    draft: dict[str, str] = {}
    try:
        form = await form_fields(request)
        draft = {
            "precision": form.get("precision", ""),
            "day": form.get("day", ""),
            "month": form.get("month", ""),
        }
        body = _parse_form(form)
        with session.begin():
            record_reading_time(
                session,
                paper_id,
                body,
                now=utc_now(),
                expected_revision=body.expected_revision,
                idempotency_key=body.idempotency_key,
            )
        paper = PaperRepository(session).get(paper_id)
        return HTMLResponse(
            _detail_article(session, paper_id, paper=paper, notice="已记录阅读时间。"),
            headers=HEADERS,
        )
    except (DomainError, AppError, ValidationError) as exc:
        message = (
            exc.message if isinstance(exc, DomainError | AppError) else "输入无效，请检查日期格式。"
        )
        status = (
            422
            if isinstance(exc, ValidationError)
            else exc.status_code
            if isinstance(exc, AppError)
            else 409
        )
        try:
            article = _detail_article(
                session,
                paper_id,
                notice=f"{message}（已保留你的输入，可修正后重试）",
                notice_role="alert",
                read_time_draft=draft,
            )
            return HTMLResponse(article, status_code=status, headers=HEADERS)
        except (DomainError, AppError):
            return HTMLResponse(
                f'<article><p role="alert">{escape(message)}</p></article>',
                status_code=status,
                headers=HEADERS,
            )
    except (ValueError, UnicodeError):
        return HTMLResponse(
            '<article><p role="alert">日期格式无效，表单须为UTF-8。</p></article>',
            status_code=422,
            headers=HEADERS,
        )


_PAGE_SIZE = 50
_SECTION_PREVIEW = 10
_SECTIONS = ("readings", "backfills", "changes", "unassigned")
_SOURCE_LABELS = {"system": "系统完成", "imported": "外部导入", "unknown": "来源未明"}
_MODE_LABELS = {ReviewMode.AS_RECORDED: "当时记录", ReviewMode.REVISED_NOW: "现在重新回顾"}


@router.get("")
def overview_page(
    session: SessionDep,
    kind: str | None = None,
    anchor_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    mode: str | None = None,
    tag: str | None = None,
    section: str = "readings",
    offset: int = 0,
) -> Response:
    """实时事实概览；无需模型与网络，GET 不产生任何写入。"""
    from papersteward.api.insights import finalize_overview, parse_overview_query
    from papersteward.reading.insights.aggregate import (
        OVERVIEW_DETAIL_MAX_LIMIT,
        build_overview,
    )
    from papersteward.reading.insights.projection import build_projection

    if get_onboarding(session).stage.value != "active":
        return RedirectResponse("/ui/setup", status_code=303)
    raw_params = {
        "kind": kind or "",
        "anchor_date": anchor_date or "",
        "start_date": start_date or "",
        "end_date": end_date or "",
        "mode": mode or "",
        "tag": tag or "",
    }
    try:
        query, tag_code = parse_overview_query(
            session,
            kind=kind,
            anchor_date=anchor_date,
            start_date=start_date,
            end_date=end_date,
            mode=mode,
            tag=tag,
        )
        selected = section if section in _SECTIONS else "readings"
        projection = build_projection(session, query)
        overview = finalize_overview(
            session,
            build_overview(projection, tag=tag_code),
            detail_limit=OVERVIEW_DETAIL_MAX_LIMIT,
        )
        body = _overview_body(
            session, overview, selected=selected, offset=max(0, offset), raw_params=raw_params
        )
        return _page(
            f"阅读回顾 · {overview.metrics.period.label}",
            body,
            nav="归档",
        )
    except (DomainError, AppError) as exc:
        rendered = _page("阅读回顾", f'<p role="alert">{escape(exc.message)}</p>', nav="归档")
        rendered.status_code = exc.status_code if isinstance(exc, AppError) else 409
        return rendered


def _overview_body(
    session: SessionDep,
    overview: InsightOverview,
    *,
    selected: str,
    offset: int,
    raw_params: dict[str, str],
) -> str:
    metrics = overview.metrics
    tz = ZoneInfo(metrics.period.timezone)
    filter_params = {key: value for key, value in raw_params.items() if value}
    page_params = {**filter_params, "section": selected}
    body = (
        '<p class="notice">实时概览，尚未保存：以下数字由当前归档记录即时计算，'
        f"知识截止 {escape(metrics.knowledge_cutoff.isoformat())} · "
        f"采集时刻 {escape(metrics.collected_at.isoformat())} · "
        f"时区 {escape(metrics.period.timezone)} · "
        f"模式 {_MODE_LABELS[metrics.mode]}</p>"
    )
    body += _filter_form(session, filter_params)
    body += "<h2>本期指标</h2>" + _metrics_table(metrics)
    body += "<h2>分布</h2>" + _distributions_html(metrics)
    sections: dict[str, tuple[str, list[Any], str]] = {
        "readings": ("阅读明细", overview.readings, "阅读时间明确属于本期的独立文献"),
        "backfills": (
            "补录明细",
            overview.backfills,
            "本期进入归档的外部已读论文；与阅读量可能重叠，不能相加",
        ),
        "changes": ("反馈修订", overview.changes, "本期发生的实际修订；初始归档不算修订"),
        "unassigned": ("未定阅读日期", overview.unassigned, "截至截止时刻阅读时间未定的归档库存"),
    }
    for name in _SECTIONS:
        heading, items, note = sections[name]
        body += _section_block(
            name,
            heading,
            items,
            note,
            tz=tz,
            selected=selected,
            offset=offset,
            page_params=page_params,
        )
    if metrics.all_history_total == 0:
        prefix = "当前筛选范围内没有归档记录。" if filter_params.get("tag") else ""
        body += (
            f'<article class="empty"><h2>{prefix or "还没有可回顾的归档"}</h2>'
            "<p>完成阅读或导入已读论文后，这里会给出阅读事实。</p>"
            '<p><a class="button secondary" href="/ui/archive">返回归档</a></p></article>'
        )
    return body


def _filter_form(session: SessionDep, current: dict[str, str]) -> str:
    taxonomy = TaxonomyRepository(session).latest_confirmed()
    tag_options: list[tuple[str, str]] = [("", "全部标签")]
    if taxonomy:
        tag_options += [
            (t.code, t.name) for t in taxonomy.tags if t.level.value == "primary" and t.active
        ]
    kind_options = [
        ("week", "本周"),
        ("month", "本月"),
        ("all", "全部历史"),
        ("custom", "自选日期"),
    ]
    return (
        '<form method="get" class="queue-filters">'
        '<label>周期<select name="kind">'
        + select_options(kind_options, selected=current.get("kind") or "week")
        + '</select></label><label>周/月所属日<input type="date" name="anchor_date" value="'
        + escape(current.get("anchor_date", ""))
        + '"></label><label>开始日<input type="date" name="start_date" value="'
        + escape(current.get("start_date", ""))
        + '"></label><label>结束日<input type="date" name="end_date" value="'
        + escape(current.get("end_date", ""))
        + '"></label><label>模式<select name="mode">'
        + select_options(
            [("as_recorded", "当时记录"), ("revised_now", "现在重新回顾")],
            selected=current.get("mode") or "as_recorded",
        )
        + '</select></label><label>主标签<select name="tag">'
        + select_options(tag_options, selected=current.get("tag") or "")
        + '</select></label><button type="submit">查看</button></form>'
    )


def _metrics_table(metrics: InsightMetrics) -> str:
    coverage = (
        f"已评分 {metrics.coverage.scored}/{metrics.coverage.total} · "
        f"有备注 {metrics.coverage.noted}/{metrics.coverage.total}"
        if metrics.coverage.total
        else "暂无"
    )
    backfill_coverage = (
        f"已评分 {metrics.backfill_coverage.scored}/{metrics.backfill_coverage.total} · "
        f"有备注 {metrics.backfill_coverage.noted}/{metrics.backfill_coverage.total}"
        if metrics.backfill_coverage.total
        else "暂无"
    )
    changes = metrics.feedback_changes
    rows = [
        (
            "本期阅读（独立文献）",
            str(metrics.readings_total),
            f"系统 {metrics.readings_system} · 导入 {metrics.readings_imported}",
        ),
        ("本期系统内完成数", str(metrics.system_completions), "辅助对照，不与阅读数相加"),
        ("本期补录", str(metrics.backfills_total), "外部已读论文进入归档；与阅读量可能重叠"),
        ("补录覆盖率", backfill_coverage, "补录集合单独展示"),
        ("阅读时间未知", str(metrics.unassigned_total), "截至截止时刻的未定日期库存"),
        ("月精度未计入", str(metrics.partial_month_total), "只知月份、周期未完整覆盖该月"),
        ("覆盖率", coverage, "本期阅读集合中截止前最后有效反馈"),
        (
            "反馈变化",
            f"评分 {changes.score_events} 次 · 备注 {changes.note_events} 次"
            f" · 涉及 {changes.papers_affected} 篇",
            "非初始修订的实际变化",
        ),
        (
            "全部历史（当前范围）",
            str(metrics.all_history_total),
            "截止前全部归档，含未知阅读日期",
        ),
    ]
    if metrics.mode is ReviewMode.REVISED_NOW and metrics.readings_backfilled:
        rows[0] = (
            rows[0][0],
            f"{rows[0][1]}（含后补 {metrics.readings_backfilled}）",
            rows[0][2],
        )
    header = (
        '<table><thead><tr><th scope="col">指标</th>'
        '<th scope="col">数值</th><th scope="col">口径</th></tr></thead><tbody>'
    )
    body = header
    for name, value, note in rows:
        body += (
            "<tr>"
            f'<th scope="row">{escape(name)}</th>'
            f'<td data-label="数值">{escape(value)}</td>'
            f'<td data-label="口径" class="muted">{escape(note)}</td>'
            "</tr>"
        )
    return body + "</tbody></table>"


def _distributions_html(metrics: InsightMetrics) -> str:
    body = "<h3>评分分布</h3>"
    if metrics.readings_total == 0:
        body += '<p class="muted">暂无</p>'
    else:
        dist = metrics.score_distribution
        buckets = [
            ("1 分", dist.score_1),
            ("2 分", dist.score_2),
            ("3 分", dist.score_3),
            ("4 分", dist.score_4),
            ("5 分", dist.score_5),
            ("未评分", dist.unrated),
        ]
        body += _count_table(buckets, "档位")
    body += "<h3>阅读方式</h3>" + (
        _entry_table(metrics.reading_modes, "方式", {"unknown": "未知"})
        or '<p class="muted">暂无</p>'
    )
    body += (
        "<h3>主题分布</h3>"
        '<p class="muted">按归档时主标签与分类版本互斥统计；同名不同版本不合并。</p>'
        + (_entry_table(metrics.primary_tags, "标签") or '<p class="muted">暂无</p>')
    )
    body += "<h3>发表年代</h3>" + (
        _entry_table(metrics.published_decades, "年代", {"unknown": "未知"})
        or '<p class="muted">暂无</p>'
    )
    return body


def _count_table(buckets: list[tuple[str, int]], label: str) -> str:
    body = (
        f'<table><thead><tr><th scope="col">{escape(label)}</th>'
        '<th scope="col">篇数</th></tr></thead><tbody>'
    )
    for name, count in buckets:
        body += f'<tr><th scope="row">{escape(name)}</th><td data-label="篇数">{count}</td></tr>'
    return body + "</tbody></table>"


def _entry_table(
    entries: list[CountEntry], label: str, key_labels: dict[str, str] | None = None
) -> str:
    if not entries:
        return ""
    key_labels = key_labels or {}
    body = (
        f'<table><thead><tr><th scope="col">{escape(label)}</th>'
        '<th scope="col">篇数</th><th scope="col">分类版本</th></tr></thead><tbody>'
    )
    for entry in entries:
        name = key_labels.get(entry.key, entry.label)
        version = entry.taxonomy_version or "—"
        body += (
            "<tr>"
            f'<th scope="row">{escape(name)}</th>'
            f'<td data-label="篇数">{entry.count}</td>'
            f'<td data-label="分类版本" class="muted">{escape(version)}</td>'
            "</tr>"
        )
    return body + "</tbody></table>"


def _section_block(
    name: str,
    heading: str,
    items: list[Any],
    note: str,
    *,
    tz: ZoneInfo,
    selected: str,
    offset: int,
    page_params: dict[str, str],
) -> str:
    total = len(items)
    window = items[offset : offset + _PAGE_SIZE] if name == selected else items[:_SECTION_PREVIEW]
    body = f'<h2>{escape(heading)}（{total}）</h2><p class="muted">{escape(note)}。</p>'
    if not window:
        body += '<p class="muted">本范围内暂无记录。</p>'
        return body
    if name == "changes":
        body += _changes_table(window)
    else:
        body += _papers_table(window, tz)
    if name == selected and total > _PAGE_SIZE:
        body += _pagination(
            "/ui/reading-insights", page_params, offset=offset, total=total, page_size=_PAGE_SIZE
        )
    return body


def _papers_table(papers: list[PaperProjection], tz: ZoneInfo) -> str:
    body = (
        '<table><thead><tr><th scope="col">文献</th><th scope="col">来源</th>'
        '<th scope="col">阅读时间</th><th scope="col">归档日</th>'
        '<th scope="col">评分</th></tr></thead><tbody>'
    )
    for paper in papers:
        title = paper.title or paper.paper_id
        reading = _reading_time_text(paper.reading)
        archived_day = paper.archived_at.astimezone(tz).date().isoformat()
        latest = paper.feedback[-1] if paper.feedback else None
        score = "未评分" if latest is None or latest.score is None else f"{latest.score} 分"
        source_label = _SOURCE_LABELS.get(paper.source.value, paper.source.value)
        body += (
            "<tr>"
            '<th scope="row"><a href="/ui/papers/'
            f'{escape(paper.paper_id)}">{escape(title)}</a></th>'
            f'<td data-label="来源">{escape(source_label)}</td>'
            f'<td data-label="阅读时间">{escape(reading)}</td>'
            f'<td data-label="归档日">{escape(archived_day)}</td>'
            f'<td data-label="评分">{escape(score)}</td>'
            "</tr>"
        )
    return body + "</tbody></table>"


def _changes_table(changes: list[Any]) -> str:
    body = (
        '<table><thead><tr><th scope="col">时间</th><th scope="col">类型</th>'
        '<th scope="col">变化</th><th scope="col">文献</th></tr></thead><tbody>'
    )
    for change in changes:
        kind_label = "评分" if change.kind == "score" else "备注"
        empty = "未评分" if change.kind == "score" else "（已清空）"
        old = empty if change.old is None else str(change.old)
        new = empty if change.new is None else str(change.new)
        body += (
            "<tr>"
            f'<th scope="row">{escape(change.recorded_at.isoformat())}</th>'
            f'<td data-label="类型">{kind_label}</td>'
            f'<td data-label="变化">{escape(old)} → {escape(new)}</td>'
            '<td data-label="文献"><a href="/ui/papers/'
            f'{escape(change.paper_id)}">{escape(change.paper_id)}</a></td>'
            "</tr>"
        )
    return body + "</tbody></table>"
