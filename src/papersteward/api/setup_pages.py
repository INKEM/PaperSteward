"""首次设置向导：连接模型 → 上传材料 → 审阅草案并确认。

页面只做表单校验和拼装，业务规则全部调用 onboarding 服务层。浏览器路径不要求
用户接触 .env：模型连接保存在工作区数据库，保存即授权数据外发。
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api import onboarding as api
from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import (
    conflict_redirect,
    failure,
    fields,
    hidden,
    input_field,
)
from papersteward.api.pages import _page
from papersteward.api.settings_pages import llm_form
from papersteward.api.workflow_pages import run_body
from papersteward.config import get_settings
from papersteward.core.entities import Run
from papersteward.core.enums import RunStatus
from papersteward.core.errors import DomainError
from papersteward.documents import parse_profile, parse_taxonomy
from papersteward.errors import AppError, InvalidRequestError
from papersteward.jobs.ui_flow import FlowRequest
from papersteward.llm.runtime import resolve_llm_config
from papersteward.onboarding.intake import MAX_MATERIALS, add_feedback
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import RunRepository

router = APIRouter(prefix="/ui", include_in_schema=False)

STEPS = ["连接模型", "上传材料", "审阅并确认"]


def steps_bar(current: int) -> str:
    items = []
    for index, name in enumerate(STEPS, 1):
        attribute = ' class="current"' if index == current else ""
        items.append(f"<li{attribute}>{escape(name)}</li>")
    return '<ol class="steps">' + "".join(items) + "</ol>"


def _model_ready(session: SessionDep) -> bool:
    return resolve_llm_config(session, get_settings()).ready


def _materials_section(summary: dict[str, Any], *, editable: bool) -> str:
    materials = summary["materials"]
    body = "<h2>研究材料</h2>"
    if materials:
        items = []
        for material in materials:
            item = f"<li>{escape(material['name'])}（{material['chars']} 字）"
            if editable:
                item += (
                    '<form method="post" action="/ui/setup/materials/delete" class="inline" '
                    'data-confirm="删除这份材料？删除后需要重新粘贴内容。">'
                    + hidden("material_id", material["id"])
                    + "<button>删除材料</button></form>"
                )
            items.append(item + "</li>")
        body += "<ul>" + "".join(items) + "</ul>"
    else:
        body += (
            "<p>还没有材料。粘贴或上传描述你研究方向的任意文字：个人主页简介、研究计划、"
            "开题报告、代表性论文摘要、平时笔记都可以。</p>"
        )
    if editable:
        body += (
            '<form method="post" action="/ui/setup/materials">'
            + '<label>材料名称（可选）<input name="name" placeholder="未命名材料"></label>'
            + '<label>材料内容<textarea name="content" rows="10" required '
            'placeholder="粘贴文字，或用下方按钮选择 .md/.txt 文件"></textarea></label>'
            + '<input type="hidden" name="from_file" value="0">'
            + '<label class="check">从文件导入（.md/.txt，可重复添加）：'
            '<input type="file" accept=".md,.markdown,.txt" data-import-target="content" '
            'data-file-marker="from_file"></label>'
            + '<span id="import-status" role="status" class="muted"></span>'
            + f'<p class="muted">最多 {MAX_MATERIALS} 份；单份表单上限 512 KiB，'
            "超长材料会提示拆分。</p>" + "<button>添加材料</button></form>"
        )
    return body


def _generate_section(session: SessionDep, summary: dict[str, Any]) -> str:
    if not _model_ready(session):
        return (
            '<p class="notice">第一步未完成：请先在「连接模型」保存 API 配置，再回来生成草案。</p>'
            '<p><a class="button" href="/ui/settings">去连接模型</a></p>'
        )
    if not summary["materials"]:
        return '<p class="notice">请先添加至少一份材料，再生成草案。</p>'
    has_drafts = summary["profile"] is not None or summary["taxonomy"] is not None
    label = "按意见修改，重新生成草案" if has_drafts else "生成草案"
    return (
        '<form method="post" action="/ui/setup/generate">'
        + hidden("run_id", "ext-" + uuid4().hex[:16])
        + f"<button>{escape(label)}</button></form>"
        + '<p class="muted">生成由后台任务执行；页面会原地显示进度，完成后自动进入审阅。</p>'
    )


def _active_run(session: SessionDep) -> Run | None:
    runs = RunRepository(session).running_ui_runs()
    return runs[0] if runs else None


def _last_extract_note(session: SessionDep) -> str:
    """最近一次草案生成的失败或停止原因；更早的历史不再展示。"""
    run = RunRepository(session).latest("ui.extract")
    if run is None:
        return ""
    if run.status is RunStatus.FAILED and run.errors_json:
        messages = [
            str(error.get("message", ""))
            for error in run.errors_json.get("errors", [])
            if isinstance(error, dict)
        ]
        text = "；".join(message for message in messages if message)
        if text:
            return f'<p role="alert">{escape(text)}</p>'
    if run.status is RunStatus.CANCELLED:
        return '<p class="notice">上一次生成已停止，已完成的数据保留。可调整材料后重新生成。</p>'
    return ""


def _tags(values: list[str]) -> str:
    return "".join(f'<span class="tag">{escape(str(value))}</span>' for value in values)


def _profile_preview(review: dict[str, Any]) -> str:
    profile = parse_profile(review["content_md"], document_id=review["id"])
    data = dict(profile.parsed_json)
    positioning = data.get("positioning") or {}
    body = '<div class="draft-preview">'
    summary = positioning.get("summary")
    if summary:
        body += f"<p>{escape(str(summary))}</p>"
    meta = [str(positioning[key]) for key in ("domain", "career_stage") if positioning.get(key)]
    if meta:
        body += f'<p class="muted">{escape(" · ".join(meta))}</p>'
    body += "<h3>核心问题</h3><ol>"
    body += "".join(f"<li>{escape(str(item['text']))}</li>" for item in data["core_questions"])
    body += "</ol><h3>子方向</h3><table><thead><tr><th>子方向</th><th>优先级</th>"
    body += "<th>目标占比</th><th>检索关键词</th></tr></thead><tbody>"
    for item in data["subdirections"]:
        body += (
            f"<tr><td>{escape(str(item['name']))}</td>"
            f"<td>{escape(str(item.get('priority', 'medium')))}</td>"
            f"<td>{float(item['target_share']):.0%}</td>"
            f"<td>{_tags(item.get('keywords', []))}</td></tr>"
        )
    body += "</tbody></table>"
    for label, key in (("纳入标准", "include"), ("排除标准", "exclude")):
        values = data.get(key) or []
        if values:
            body += f"<h3>{label}</h3><ul>"
            body += "".join(f"<li>{escape(str(value))}</li>" for value in values)
            body += "</ul>"
    reading = data.get("reading") or {}
    ratio = reading.get("deep_skim_ratio") or [0, 1]
    body += (
        "<h3>阅读节奏</h3><p>"
        f"每周 {escape(str(reading.get('weekly_capacity', '—')))} 篇，"
        f"精读/粗读约 {float(ratio[0]):.0%}:{float(ratio[1]):.0%}。"
        "</p>"
    )
    goals = reading.get("reading_goals") or []
    if goals:
        body += f"<p>目标：{_tags(goals)}</p>"
    return body + "</div>"


def _taxonomy_preview(review: dict[str, Any]) -> str:
    taxonomy = parse_taxonomy(review["content_md"], document_id=review["id"])
    body = '<div class="draft-preview">'
    for tag in taxonomy.tags:
        if tag.level.value != "primary":
            continue
        body += f"<details><summary>{escape(tag.code)} {escape(tag.name)}</summary>"
        body += f"<p>{escape(tag.definition)}</p>"
        body += "<p><strong>纳入</strong></p><ul>"
        body += "".join(f"<li>{escape(value)}</li>" for value in tag.include)
        body += "</ul><p><strong>排除</strong></p><ul>"
        body += "".join(f"<li>{escape(value)}</li>" for value in tag.exclude)
        body += "</ul><p><strong>正例</strong> " + _tags(list(tag.examples)) + "</p></details>"
    return body + "</div>"


def _draft_card(title: str, review: dict[str, Any], preview: str) -> str:
    body = (
        f'<article><h2>{escape(title)} <span class="muted">v{escape(review["version"])}</span></h2>'
    )
    if review.get("missing_fields"):
        body += (
            '<p class="notice">还需补充：'
            + escape("、".join(str(field) for field in review["missing_fields"]))
            + "。可以提意见让 Agent 补全。</p>"
        )
    body += (
        "<details open><summary>渲染视图</summary>"
        + preview
        + "</details><details><summary>Markdown 源码</summary><pre>"
        + escape(str(review["content_md"]))
        + "</pre></details>"
    )
    if review.get("markdown_diff"):
        body += (
            "<details><summary>与上一版的差异</summary><pre>"
            + escape(str(review["markdown_diff"]))
            + "</pre></details>"
        )
    return body + "</article>"


def _draft_section(summary: dict[str, Any]) -> str:
    profile, taxonomy = summary["profile"], summary["taxonomy"]
    if profile is None and taxonomy is None:
        return (
            '<p class="notice">还没有草案。素材就绪后点击下方按钮，由 Agent 生成研究方向档案'
            "和分类标签。</p>"
        )
    body = "<h2>最新草案</h2>"
    if profile is not None:
        try:
            preview = _profile_preview(profile)
        except (DomainError, ValueError, KeyError):
            preview = '<p class="notice">渲染预览失败，请查看 Markdown 源码。</p>'
        body += _draft_card("研究方向档案", profile, preview)
    if taxonomy is not None:
        try:
            preview = _taxonomy_preview(taxonomy)
        except (DomainError, ValueError, KeyError):
            preview = '<p class="notice">渲染预览失败，请查看 Markdown 源码。</p>'
        body += _draft_card("分类标签", taxonomy, preview)
    return body


def _feedback_and_confirm(session: SessionDep, summary: dict[str, Any]) -> str:
    if summary["profile"] is None and summary["taxonomy"] is None:
        return ""
    body = "<h2>提意见，直到满意</h2>"
    for comment in summary["feedback"]:
        body += f'<p class="muted">已提意见：{escape(comment)}</p>'
    if _model_ready(session):
        body += (
            '<form method="post" action="/ui/setup/feedback">'
            + hidden("run_id", "ext-" + uuid4().hex[:16])
            + "<label>修改意见（例如“方向太窄，把 XX 也加进去”“不要子方向 S3”）"
            '<textarea name="comment" rows="3" required></textarea></label>'
            + "<button>提交意见并重新生成</button></form>"
        )
    else:
        body += (
            '<p class="notice">模型连接不可用，暂时无法按意见重新生成。'
            '请先到 <a href="/ui/settings">设置</a> 检查模型配置。</p>'
        )
    if summary["ready_to_confirm"]:
        profile, taxonomy = summary["profile"], summary["taxonomy"]
        confirm_body = (
            hidden("expected_revision", get_onboarding(session).revision)
            + hidden("profile_id", profile["id"])
            + hidden("profile_version", profile["version"])
            + hidden("profile_hash", profile["content_hash"])
            + hidden("taxonomy_id", taxonomy["id"])
            + hidden("taxonomy_version", taxonomy["version"])
            + hidden("taxonomy_hash", taxonomy["content_hash"])
            + input_field("confirmed_by", "local-user", "确认人")
            + input_field("change_summary", "已审阅研究草案，确认开始使用", "确认说明")
            + '<label class="check"><input type="checkbox" name="confirmed" value="yes" required>'
            "我已审阅以上草案，确认开始使用</label>"
        )
        body += (
            '<form method="post" action="/ui/setup/confirm">'
            + confirm_body
            + "<button>确认，开始使用</button></form>"
        )
    else:
        body += (
            '<p class="notice">草案完整后即可确认。若缺少字段，请在意见中说明，让 Agent 补充。</p>'
        )
    return body


def _legacy_section(summary: dict[str, Any]) -> str:
    legacy = summary.get("legacy_drafts") or []
    if not legacy:
        return ""
    labels = {"profile": "研究方向档案", "taxonomy": "分类标签"}
    items = "".join(
        f"<li>{labels.get(item['kind'], item['kind'])}："
        f"{escape(item['id'])} v{escape(item['version'])}（{escape(item['status'])}）</li>"
        for item in legacy
    )
    return (
        "<details><summary>从旧向导导入遗留草稿</summary>"
        "<p>检测到旧版向导留下的资料。导入后作为本次材料，由 Agent 重新整理：</p>"
        f"<ul>{items}</ul>"
        '<form method="post" action="/ui/setup/import-legacy"><button>导入为材料</button></form>'
        "</details>"
    )


def confirmed_results(session: SessionDep) -> str:
    """确认后的研究档案与分类规则继续可见，复用已有安全渲染。"""
    from papersteward.onboarding.setup import RUNTIME_KEY
    from papersteward.storage.repositories import (
        ConfigRepository,
        ProfileRepository,
        TaxonomyRepository,
    )

    profile = ProfileRepository(session).latest_confirmed()
    taxonomy = TaxonomyRepository(session).latest_confirmed()
    if profile is None or taxonomy is None:
        return (
            "<article><h2>研究档案与分类标签</h2><p>尚未完成首次设置。</p>"
            '<a href="/ui/setup">开始首次设置</a></article>'
        )
    profile_data = {"id": profile.id, "version": profile.version, "content_md": profile.content_md}
    taxonomy_data = {
        "id": taxonomy.id,
        "version": taxonomy.version,
        "content_md": taxonomy.content_md,
    }
    body = "<h2>当前使用的研究设置</h2>"
    body += _draft_card("已确认的研究方向档案", profile_data, _profile_preview(profile_data))
    body += _draft_card("已确认的分类标签", taxonomy_data, _taxonomy_preview(taxonomy_data))
    runtime = ConfigRepository(session).latest(RUNTIME_KEY)
    if runtime is not None:
        body += (
            '<p class="muted">工作区时区：'
            + escape(str(runtime.value_json.get("timezone", "")))
            + "。挖掘由你手动发起，阅读容量仅作建议。</p>"
        )
    return body


@router.get("/setup")
def setup(
    session: SessionDep,
    reset: bool = False,
    added: bool = False,
    removed: bool = False,
    imported: bool = False,
) -> HTMLResponse:
    try:
        flashes = ""
        if reset:
            flashes += (
                '<p class="notice success">工作区已重置，原始数据库备份保存在数据目录的 '
                "backups/，可重新开始首次设置。</p>"
            )
        if added:
            flashes += '<p class="notice success">材料已添加，生成草案时会合并全部材料。</p>'
        if removed:
            flashes += '<p class="notice success">材料已删除。</p>'
        if imported:
            flashes += '<p class="notice success">遗留草稿已导入为材料。</p>'
        state = get_onboarding(session)
        stage = state.stage.value
        if stage == "active":
            body = (
                steps_bar(3) + "<p>首次设置已完成。可以随时在 "
                '<a href="/ui/settings">设置</a> 中调整模型连接。</p>'
                '<p><a class="button" href="/ui/today">进入阅读队列</a> '
                '<a class="button secondary" href="/ui/settings">模型连接</a></p>'
            )
            return _page("首次设置", body + confirmed_results(session))

        summary = api.read_drafts(session)
        ready = _model_ready(session)
        if stage == "setup":
            current = 2 if ready else 1
            body = steps_bar(current)
            if not ready:
                body += (
                    '<p class="notice">第一步：连接模型。填写 API 地址、密钥与模型名并保存；'
                    "保存即表示同意把论文标题与摘要发送给该服务。</p>"
                )
                config = resolve_llm_config(session, get_settings())
                if config.requires_api_key and not config.has_api_key:
                    body += (
                        '<p class="notice">当前 API 地址不是本机，保存前必须填写密钥；'
                        "保存后点「测试连接」确认可用。</p>"
                    )
                body += llm_form(
                    config.base_url,
                    config.model or "",
                    config.timeout_s,
                    compact=True,
                    next_url="/ui/setup",
                    key_saved=config.has_api_key,
                    key_from_env=config.api_key_from_env,
                )
            else:
                body += (
                    '<p class="notice success">模型已连接。'
                    '需要更换时可到 <a href="/ui/settings">设置</a> 修改。</p>'
                )
            body += _legacy_section(summary)
            body += _materials_section(summary, editable=True)
            body += _generate_section(session, summary)
        else:  # draft
            body = steps_bar(3)
            active = _active_run(session)
            if active is not None:
                # 生成中：单页面只显示进度，完成后片段自动把整页刷成审阅界面。
                body += run_body(active)
            elif summary["profile"] is None and summary["taxonomy"] is None:
                body += _last_extract_note(session)
                body += _materials_section(summary, editable=True)
                body += _generate_section(session, summary)
                body += _legacy_section(summary)
            else:
                body += _draft_section(summary)
                body += _feedback_and_confirm(session, summary)
                body += (
                    "<details><summary>补充或删除材料</summary>"
                    + _materials_section(summary, editable=True)
                    + _generate_section(session, summary)
                    + "</details>"
                )
                body += _legacy_section(summary)
        return _page("首次设置", flashes + body)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)


def _enqueue_extraction(request: Request, run_id: str) -> str:
    request.app.state.ui_flows.submit(FlowRequest(run_id=run_id, kind="extract"))
    return run_id


def _run_id(data: dict[str, str]) -> str:
    value = data.get("run_id") or ""
    if value.startswith("ext-"):
        return value
    return "ext-" + uuid4().hex[:16]


@router.post("/setup/materials")
async def add_material_action(request: Request, session: SessionDep) -> Response:
    try:
        data = await fields(request)
        api.create_material(
            api.MaterialRequest(
                name=data.get("name") or "未命名材料",
                content=data.get("content", ""),
                kind="file" if data.get("from_file") == "1" else "text",
            ),
            session,
        )
        return RedirectResponse("/ui/setup", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)


@router.post("/setup/materials/delete")
async def delete_material_action(request: Request, session: SessionDep) -> Response:
    try:
        data = await fields(request)
        api.delete_material(data.get("material_id", ""), session)
        return RedirectResponse("/ui/setup?removed=1", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)


@router.post("/setup/import-legacy")
async def import_legacy_action(request: Request, session: SessionDep) -> Response:
    try:
        await fields(request)
        api.import_legacy(session)
        return RedirectResponse("/ui/setup?imported=1", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)


@router.post("/setup/generate")
async def generate_action(request: Request, session: SessionDep) -> Response:
    try:
        data = await fields(request)
        _enqueue_extraction(request, _run_id(data))
        return RedirectResponse("/ui/setup", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        redirected = conflict_redirect(exc)
        if redirected is not None:
            return redirected
        return failure(exc)


@router.post("/setup/feedback")
async def feedback_action(request: Request, session: SessionDep) -> Response:
    try:
        data = await fields(request)
        if running := RunRepository(session).running_ui_runs():
            # 有任务在跑：意见不落库，直接带用户去看进度，避免"已保存却不生成"。
            return RedirectResponse(f"/ui/runs/{quote(running[0].id, safe='')}", status_code=303)
        session.rollback()  # 检查开启的只读事务先结束，意见用下面的显式事务写入
        with session.begin():
            add_feedback(session, data.get("comment", ""))
        _enqueue_extraction(request, _run_id(data))
        return RedirectResponse("/ui/setup", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        redirected = conflict_redirect(exc)
        if redirected is not None:
            return redirected
        return failure(exc)


@router.post("/setup/confirm")
async def confirm_action(request: Request, session: SessionDep) -> Response:
    try:
        data = await fields(request)
        if data.get("confirmed") != "yes":
            raise InvalidRequestError("请勾选确认后再开始使用")
        api.confirm(
            api.ConfirmRequest(
                confirmed=True,
                confirmed_by=data.get("confirmed_by", "local-user"),
                change_summary=data.get("change_summary", "已确认"),
                expected_revision=int(data.get("expected_revision", "1")),
                profile_id=data.get("profile_id", ""),
                profile_version=data.get("profile_version", ""),
                profile_hash=data.get("profile_hash", ""),
                taxonomy_id=data.get("taxonomy_id", ""),
                taxonomy_version=data.get("taxonomy_version", ""),
                taxonomy_hash=data.get("taxonomy_hash", ""),
            ),
            session,
        )
        return RedirectResponse("/ui/today", status_code=303)
    except (DomainError, AppError, ValidationError, ValueError) as exc:
        return failure(exc)
