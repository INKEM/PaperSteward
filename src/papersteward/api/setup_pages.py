"""Browser onboarding: validate form input, then call the existing API/service boundary."""

from __future__ import annotations

import json
from html import escape
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api import onboarding as api
from papersteward.api.deps import SessionDep
from papersteward.api.pages import HEADERS, STATIC, _guard, _page
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, InvalidRequestError
from papersteward.onboarding.taxonomy import _evidence
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository, TaxonomyRepository

router = APIRouter(prefix="/ui", include_in_schema=False)


def starter(name: str) -> str:
    if name not in {
        "profile.md",
        "taxonomy.md",
        "seeds.json",
        "calibration.json",
        "dry_run.json",
        "scoring.yaml",
    }:
        raise InvalidRequestError("未知示例资源")
    return files("papersteward.resources").joinpath("starter", name).read_text(encoding="utf-8")


def hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{escape(name)}" value="{escape(str(value))}">'


def form(action: str, body: str, label: str) -> str:
    return (
        f'<form method="post" action="/ui/setup/{action}">{body}'
        f'<button type="submit">{escape(label)}</button></form>'
    )


def textarea(name: str, value: str, label: str) -> str:
    return (
        f'<label for="{name}">{escape(label)}</label><textarea id="{name}" name="{name}" '
        f'rows="16" required>{escape(value)}</textarea>'
    )


def input_field(name: str, value: str, label: str) -> str:
    return f'<label>{escape(label)}<input name="{name}" value="{escape(value)}" required></label>'


def failure(exc: Exception, *, back: str = "/ui/setup") -> HTMLResponse:
    if isinstance(exc, ValidationError):
        details = "；".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()
        )
        message = "输入无效：" + details
        status = 422
    elif isinstance(exc, DomainError | AppError):
        message = exc.message
        status = exc.status_code if isinstance(exc, AppError) else 409
    else:
        message, status = "输入格式无效，请检查 JSON 或表单后重试。", 422
    page = _page(
        "请检查后重试",
        f'<p role="alert">{escape(message)}</p><a href="{escape(back)}">返回页面检查后重试</a>',
    )
    page.status_code = status
    return page


async def fields(request: Request) -> dict[str, str]:
    _guard(request)
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise InvalidRequestError("请使用页面表单提交")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 512 * 1024:
            raise InvalidRequestError("表单过大，最多 512 KiB")
    return {
        key: values[-1]
        for key, values in parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=32).items()
    }


def approval(data: dict[str, str]) -> dict[str, Any]:
    return {
        "confirmed": data.get("confirmed") == "yes",
        "confirmed_by": data.get("confirmed_by", ""),
        "change_summary": data.get("change_summary", ""),
        "expected_revision": data.get("expected_revision", ""),
    }


def confirmation_fields(review: dict[str, Any], revision: int) -> str:
    body = hidden("expected_revision", revision)
    for key in ("content_hash", "review_hash", "report_hash", "feedback_hash"):
        if review.get(key):
            body += hidden(key, review[key])
    body += input_field("confirmed_by", "local-user", "确认人")
    body += input_field("change_summary", "已审阅并确认本步骤内容", "确认说明")
    body += (
        '<label class="check"><input type="checkbox" name="confirmed" value="yes" required>'
        "我已审阅以上内容，明确确认</label>"
    )
    return body


def _documents(
    session: SessionDep, kind: str, identity: str | None, version: str | None, edit: bool
) -> str:
    repository = ProfileRepository(session) if kind == "profile" else TaxonomyRepository(session)
    documents = repository.list()
    body: str = (
        '<p class="notice">Starter 为示例/合成资料。请改成自己的研究方向；示例指标只验证流程。</p>'
    )
    for document in documents:
        query = urlencode({"kind": kind, "identity": document.id, "version": document.version})
        body += (
            f"<p>{escape(document.id)} / {escape(document.version)}："
            f'<a href="/ui/setup?{escape(query)}">审阅</a> · '
            f'<a href="/ui/setup?{escape(query)}&amp;edit=true">编辑为新版本</a></p>'
        )
    state = get_onboarding(session)
    review = None
    if identity and version:
        review = (
            api.review_profile(identity, version, session)
            if kind == "profile"
            else api.review_taxonomy(identity, version, session)
        )
    if review is not None and not edit:
        body += "<article><h2>全文审阅</h2><pre>" + escape(review["content_md"]) + "</pre>"
        details = {
            key: review[key]
            for key in ("missing_fields", "questions", "report", "diff", "markdown_diff")
            if key in review and review[key] is not None
        }
        body += (
            "<h2>校验与影响</h2><pre>"
            + escape(json.dumps(details, ensure_ascii=False, indent=2))
            + "</pre></article>"
        )
        confirm = confirmation_fields(review, state.revision)
        confirm += hidden("identity", review["id"]) + hidden("version", review["version"])
        return body + form(
            kind + "/confirm", confirm, "确认 " + ("G1" if kind == "profile" else "G2")
        )
    content = review["content_md"] if review else starter(kind + ".md")
    default_id = identity or ("demo-profile" if kind == "profile" else "onboarding-taxonomy")
    editor = input_field("identity", default_id, "文档 ID")
    editor += hidden("base_version", version if review else "")
    editor += "<p>编辑已有版本时，请在 Markdown 中提升 version；旧版本会保留。</p>"
    editor += textarea(
        "content_md", content, "研究方向 Markdown" if kind == "profile" else "分类法 Markdown"
    )
    if kind == "taxonomy":
        seeds = starter("seeds.json")
        sample_kind = "synthetic"
        if review:
            taxonomy = TaxonomyRepository(session).get(review["id"], review["version"])
            assert taxonomy is not None
            proof = _evidence(session, taxonomy)
            seeds = json.dumps(proof["seeds"], ensure_ascii=False, indent=2)
            sample_kind = proof["sample_kind"]
        editor += (
            '<label>资料性质<select name="sample_kind">'
            f'<option value="synthetic"{" selected" if sample_kind == "synthetic" else ""}>'
            "示例/合成</option>"
            f'<option value="user"{" selected" if sample_kind == "user" else ""}>'
            "我的真实资料</option></select></label>"
        )
        editor += (
            '<label>导入种子 JSON 文件<input type="file" accept=".json,application/json" '
            'data-import-target="seeds"></label>'
        )
        editor += textarea("seeds", seeds, "种子 JSON（paper_id/title/primary_tag）")
    return body + form(kind + "/draft", editor, "保存草稿并审阅")


@router.get("/static/setup.js")
def setup_script() -> FileResponse:
    return FileResponse(STATIC / "setup.js", media_type="application/javascript", headers=HEADERS)


@router.get("/setup")
def setup(
    session: SessionDep,
    kind: str | None = None,
    identity: str | None = None,
    version: str | None = None,
    edit: bool = False,
) -> HTMLResponse:
    try:
        state = get_onboarding(session)
        body = (
            '<ol class="steps"><li>G1 研究方向</li><li>G2 分类法</li>'
            "<li>G3 校准</li><li>G4 试运行</li></ol>"
        )
        body += f"<p>当前：{escape(state.stage.value)} · revision {state.revision}</p>"
        stage = state.stage.value
        if stage == "seeds":
            body += (
                "<p>先审阅研究方向与标签，再用校准和试运行确认设置。"
                "未完成前不会正式入队或归档。</p>"
            )
            body += form("start", "", "开始设置")
        elif stage in {"profile", "taxonomy"}:
            if kind and kind != stage:
                raise InvalidRequestError("请按当前步骤操作")
            body += _documents(session, stage, identity, version, edit)
        elif stage == "active":
            body += (
                "<p>初始化已完成。示例校准仅验证流程；正式使用请在独立新工作区确认自己的资料。</p>"
                "<p>下一步：配置 LLM 和采集数据。缺少真实模型配置时不能处理真实论文。</p>"
                "<p>若选择不授权外发，当前工作区仅作离线体验；可用 --data-dir 新目录重新设置。</p>"
                '<a href="/ui/today">进入今日</a>'
            )
        elif stage == "calibration":
            from papersteward.api.setup_late import calibration_page

            body += calibration_page(session)
        elif stage == "dry_run":
            from papersteward.api.setup_late import dry_run_page

            body += dry_run_page(session)
        return _page("首次设置", body)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)


@router.post("/setup/{kind}/{action}")
async def document_action(
    kind: str, action: str, request: Request, session: SessionDep
) -> Response:
    try:
        data = await fields(request)
        if kind in {"calibration", "dry-run"}:
            from papersteward.api.setup_late import advance_action

            return advance_action(kind, action, data, session)
        identity = data.get("identity", "")
        if kind not in {"profile", "taxonomy"}:
            raise InvalidRequestError("未知文档类型")
        if action == "draft":
            body = {
                "content_md": data.get("content_md", ""),
                "base_version": data.get("base_version") or None,
            }
            if kind == "profile":
                review = api.create_profile_draft(
                    api.DraftRequest(profile_id=identity, **body), session
                )
            else:
                review = api.create_taxonomy_draft(
                    api.TaxonomyDraftRequest(
                        taxonomy_id=identity,
                        seeds=json.loads(data.get("seeds", "")),
                        sample_kind=data.get("sample_kind"),
                        **body,
                    ),
                    session,
                )
            query = urlencode(
                {"kind": kind, "identity": review["id"], "version": review["version"]}
            )
            return RedirectResponse("/ui/setup?" + query, status_code=303)
        if action == "confirm":
            body = {**approval(data), "content_hash": data.get("content_hash", "")}
            if kind == "profile":
                api.confirm_profile_g1(
                    identity, data.get("version", ""), api.ConfirmProfileRequest(**body), session
                )
            else:
                api.confirm_taxonomy_g2(
                    identity,
                    data.get("version", ""),
                    api.ConfirmTaxonomyRequest(review_hash=data.get("review_hash", ""), **body),
                    session,
                )
            return RedirectResponse("/ui/setup", status_code=303)
        raise InvalidRequestError("未知操作")
    except (DomainError, AppError, ValidationError, ValueError, UnicodeError) as exc:
        return failure(exc)


@router.post("/setup/start")
async def begin(request: Request, session: SessionDep) -> Response:
    try:
        await fields(request)
        api.start_onboarding(session)
        return RedirectResponse("/ui/setup", status_code=303)
    except (DomainError, AppError, ValueError) as exc:
        return failure(exc)
