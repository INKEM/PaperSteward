"""阅读回顾的浏览器表单；详情页阅读时间块与补记提交共用此模块。

写操作沿用全站规范：``form_fields`` 做来源检查，成功/失败都重渲染
详情正文片段（htmx 原地替换），失败保留用户已填输入（X-01）。
"""

from html import escape
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import fields as form_fields
from papersteward.api.html import HTML_HEADERS as HEADERS
from papersteward.api.html import select_options
from papersteward.core.entities import utc_now
from papersteward.core.enums import ReadingTimeBasis, TimePrecision
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.reading.insights.contracts import (
    ReadTimeInput,
    ReadTimeRevision,
    ReadTimeView,
)
from papersteward.reading.insights.timeline import record_reading_time
from papersteward.storage.repositories import PaperRepository

router = APIRouter(prefix="/ui/reading-insights", include_in_schema=False)

_PRECISION_OPTIONS = (
    ("day", "按具体日期"),
    ("month", "只记得月份"),
    ("unknown", "未知（清除已记录的日期）"),
)


def reading_time_label(view: ReadTimeView) -> str:
    """阅读时间的展示标签；系统回退值注明来源，未声明即如实显示未记录。"""
    reading = view.effective
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
