"""导入已读论文的两步表单：标识解析（可选）→ 确认元数据与主观评分。

- 入口在归档页头部；低频动作不进主导航（§3.7）。
- 解析失败/超时不阻塞：进入手填表单并说明（§3.3）；两步都走去重守卫，
  resolve 命中给详情页指引，confirm 是最终守卫。
- 纯表单无 JS；校验错误按 X-01 原地重渲染（prefill），AppError 走错误页；
  步骤②状态全部由表单字段承载（POST 直接渲染，无需服务端暂存）。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import PageError, failure, fields, hidden, json_details
from papersteward.api.html import page as _page
from papersteward.api.html import select_options
from papersteward.core.entities import utc_now
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.reading.import_service import (
    guard_duplicate,
    import_read_paper,
    resolve_identifier,
)
from papersteward.sources.contracts import PaperCandidate, SourceError
from papersteward.storage.repositories import TaxonomyRepository

router = APIRouter(prefix="/ui", include_in_schema=False)

_MANUAL_HINT = "暂时无法自动解析，请手动填写论文信息。"
_MISS_HINT = "没有从来源找到这篇论文；可以检查标识符后重试，或直接手动填写。"
_CONFIRM_FIELDS = (
    "title",
    "authors",
    "venue",
    "published_year",
    "url",
    "abstract",
    "doi",
    "arxiv_id",
    "score",
    "note",
    "primary_tag",
    "identifier_input",
    "resolve",
)


def _conflict_page(exc: ConflictError) -> HTMLResponse:
    """重复导入的冲突页：守卫文案 + 详情页链接（§3.4「+ 详情页链接」）。"""
    details = exc.details if isinstance(exc.details, dict) else {}
    detail_url = str(details.get("detail_url") or "")
    body = f'<p role="alert">{escape(exc.message)}</p>'
    if detail_url:
        body += f'<p><a class="button secondary" href="{escape(detail_url)}">打开论文详情页</a></p>'
    body += (
        '<p class="muted"><a href="/ui/import">返回导入页</a> · '
        '<a href="/ui/archive">返回归档</a></p>'
    )
    rendered = _page("无法导入这篇论文", body, nav="归档")
    rendered.status_code = exc.status_code
    return rendered


def _entry_body() -> str:
    return (
        "<article><h2>第一步 · 标识论文（可选）</h2>"
        '<p class="muted">粘贴 DOI、arXiv ID 或论文链接（doi.org / arxiv.org 链接也可以），'
        "系统会尝试自动补全标题、作者等信息。没有标识符也没关系，留空直接继续即可手动填写。</p>"
        '<form method="post" action="/ui/import/resolve" class="stacked-form">'
        "<label>DOI / arXiv ID / 论文链接"
        '<input name="identifier" inputmode="url" '
        'placeholder="例如 10.1038/nature12373 或 arxiv.org/abs/2401.00001"></label>'
        '<button type="submit">解析并继续</button></form></article>'
    )


@router.get("/import")
def import_entry() -> HTMLResponse:
    return _page("导入已读论文", _entry_body(), nav="归档")


def _prefill_from(candidate: PaperCandidate) -> dict[str, str]:
    published_at = candidate.published_at
    return {
        "title": candidate.title,
        "authors": ", ".join(candidate.authors),
        "venue": candidate.venue or "",
        "published_year": str(published_at.year) if published_at else "",
        "url": candidate.url or "",
        "abstract": candidate.abstract or "",
        "doi": candidate.doi or "",
        "arxiv_id": candidate.arxiv_id or "",
    }


def _confirm_body(
    session: Session, values: dict[str, str], *, error: str = "", hint: str = ""
) -> str:
    """第二步表单；values 为已填字段（prefill），error 为校验错误、hint 为解析说明。"""

    def text_field(name: str, label: str, *, required: bool = False, kind: str = "text") -> str:
        required_attr = " required" if required else ""
        return (
            f"<label>{escape(label)}"
            f'<input type="{kind}" name="{name}" value="{escape(values.get(name, ""))}"'
            f"{required_attr}></label>"
        )

    taxonomy = TaxonomyRepository(session).latest_confirmed()
    tag_options: list[tuple[str, str]] = [("", "不选标签")]
    if taxonomy:
        tag_options += [
            (tag.code, tag.name)
            for tag in taxonomy.tags
            if tag.level.value == "primary" and tag.active
        ]
    body = ""
    if error:
        body += f'<p role="alert">{escape(error)}</p>'
    if hint:
        body += f'<p class="notice">{escape(hint)}</p>'
    body += (
        "<article><h2>第二步 · 确认信息与评分</h2>"
        '<form method="post" action="/ui/import/confirm" class="stacked-form">'
        + text_field("title", "论文标题", required=True)
        + text_field("authors", "作者（逗号分隔）")
        + text_field("venue", "发表 venue")
        + text_field("published_year", "发表年份", kind="number")
        + text_field("url", "原文链接")
        + text_field("doi", "DOI")
        + text_field("arxiv_id", "arXiv ID")
        + '<label>摘要<textarea name="abstract" rows="6">'
        + escape(values.get("abstract", ""))
        + "</textarea></label>"
        + '<label>我的评分（必填）<select name="score" required>'
        + select_options(
            [("", "请选择评分"), *[(str(n), f"{n} 分") for n in (5, 4, 3, 2, 1)]],
            values.get("score") or None,
        )
        + "</select></label>"
        + '<label>备注（可选）<textarea name="note" rows="3">'
        + escape(values.get("note", ""))
        + "</textarea></label>"
        + '<label>主标签（可选，来自当前分类法）<select name="primary_tag">'
        + select_options(tag_options, values.get("primary_tag") or "")
        + "</select></label>"
        + hidden("identifier_input", values.get("identifier_input", ""))
        + hidden("resolve", values.get("resolve", ""))
        + '<button type="submit">导入并归档</button></form>'
        '<p class="muted">导入即声明这篇论文已读完：它会直接进入归档，'
        "之后挖掘不会再重复处理它；评分会参与偏好画像。</p></article>"
    )
    return body


@router.post("/import/resolve")
async def import_resolve(request: Request, session: SessionDep) -> Response:
    data: dict[str, str] = {}
    try:
        data = await fields(request)
        raw = (data.get("identifier") or "").strip()
        resolved = None
        hint = ""
        if raw:
            try:
                resolved = resolve_identifier(raw, now=utc_now())
            except SourceError:
                hint = _MANUAL_HINT
            if resolved is None and not hint:
                hint = _MISS_HINT
            if resolved is not None:
                guard_duplicate(
                    session,
                    title=resolved.candidate.title,
                    doi=resolved.candidate.doi,
                    arxiv_id=resolved.candidate.arxiv_id,
                )
    except PageError as exc:
        if isinstance(exc, ConflictError):
            return _conflict_page(exc)
        return failure(exc, back="/ui/import")
    values = _prefill_from(resolved.candidate) if resolved is not None else {}
    values["identifier_input"] = raw
    values["resolve"] = (
        json.dumps(resolved.resolve_payload(), ensure_ascii=False) if resolved is not None else ""
    )
    return _page("导入已读论文", _confirm_body(session, values, hint=hint), nav="归档")


def _parse_score(raw: str | None) -> int:
    try:
        return int((raw or "").strip())
    except ValueError:
        raise InvalidRequestError("请选择 1–5 的评分") from None


def _confirm_payload(data: dict[str, str], session: Session) -> ImportReadPaperInput:
    if not (data.get("title") or "").strip():
        raise InvalidRequestError("请填写论文标题")
    year_text = (data.get("published_year") or "").strip()
    published_at = None
    if year_text:
        try:
            year = int(year_text)
        except ValueError:
            raise InvalidRequestError("发表年份应是数字，例如 2024") from None
        if not 1500 <= year <= 2100:
            raise InvalidRequestError("发表年份应在 1500–2100 之间")
        published_at = datetime(year, 1, 1, tzinfo=UTC)
    authors = [
        name.strip() for name in re.split(r"[,，;；]", data.get("authors") or "") if name.strip()
    ]
    resolve = None
    if (data.get("resolve") or "").strip():
        try:
            parsed = json.loads(data["resolve"])
        except json.JSONDecodeError:
            parsed = None
        resolve = parsed if isinstance(parsed, dict) else None
    primary_tag = (data.get("primary_tag") or "").strip() or None
    if primary_tag is not None:
        taxonomy = TaxonomyRepository(session).latest_confirmed()
        codes = {tag.code for tag in taxonomy.tags} if taxonomy else set()
        if primary_tag not in codes:
            raise InvalidRequestError("所选标签不在当前分类法中，请返回重选")
    return ImportReadPaperInput.model_validate(
        {
            "title": data.get("title") or "",
            "score": _parse_score(data.get("score")),
            "note": (data.get("note") or "").strip() or None,
            "authors": authors,
            "venue": (data.get("venue") or "").strip() or None,
            "published_at": published_at,
            "url": (data.get("url") or "").strip() or None,
            "abstract": (data.get("abstract") or "").strip() or None,
            "doi": (data.get("doi") or "").strip() or None,
            "arxiv_id": (data.get("arxiv_id") or "").strip() or None,
            "identifier_input": (data.get("identifier_input") or "").strip() or None,
            "resolve": resolve,
            "primary_tag": primary_tag,
        }
    )


@router.post("/import/confirm")
async def import_confirm(request: Request, session: SessionDep) -> Response:
    data: dict[str, str] = {}
    try:
        data = await fields(request)
        with session.begin():
            payload = _confirm_payload(data, session)
            paper_id = import_read_paper(session, payload, now=utc_now())
    except PageError as exc:
        if isinstance(exc, ConflictError):
            return _conflict_page(exc)
        # X-01：校验错误原地重渲染，保留已填内容。
        values = {name: data.get(name, "") for name in _CONFIRM_FIELDS}
        rendered = _page(
            "导入已读论文",
            _confirm_body(session, values, error=json_details(exc)),
            nav="归档",
        )
        rendered.status_code = 400
        return rendered
    return RedirectResponse(f"/ui/papers/{quote(paper_id, safe='')}?imported=1", status_code=303)


__all__ = ["router"]
