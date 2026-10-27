"""研究记忆 JSON 与浏览器入口；私人表单仅 POST，不写入 URL。"""

import json
from html import escape
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, Response
from pydantic import Field
from sqlalchemy.orm import Session

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.api.form_utils import (
    PageError,
    conflict_redirect,
    failure,
    fields,
    hidden,
    json_details,
)
from papersteward.api.insights import AnalysisSubmitInput
from papersteward.api.insights_pages import _page
from papersteward.api.workspace_runtime import resolve_workspace
from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.errors import InvalidRequestError, NotAuthorizedError, NotFoundError
from papersteward.reading.insights.assets import list_assets
from papersteward.reading.insights.authorization import (
    active_grant_for_purpose,
    analysis_authorization_status,
    grant_analysis_authorization,
    revoke_analysis_authorization,
)
from papersteward.reading.insights.contracts import (
    MATERIAL_KIND_LABELS,
    MATERIAL_KIND_LIMITS,
    AnalysisGrantInput,
    MaterialExclusion,
)
from papersteward.reading.insights.materials import (
    consume_preview_token,
    create_preview_token,
    resolve_material_content,
    store_manifest,
    validate_preview_token,
)
from papersteward.reading.insights.memory import (
    decide_memory,
    delete_memory,
    get_memory,
    list_memory,
    memory_revision_view,
)
from papersteward.reading.insights.memory_contracts import (
    MEMORY_PURPOSE,
    STATUS_LABELS,
    MemoryDecisionInput,
    MemoryManifestInput,
    MemoryRevisionView,
)
from papersteward.reading.insights.memory_evidence import build_memory_manifest
from papersteward.storage.insights import ReadingAnalysisManifestRepository
from papersteward.storage.onboarding import ensure_active, get_onboarding
from papersteward.storage.reading_memory import ReadingMemoryRepository
from papersteward.storage.tx import write_tx

router = APIRouter(prefix="/reading-insights/memory", tags=["reading-insights"])
pages = APIRouter(prefix="/ui/reading-insights/memory", include_in_schema=False)
ROOT = "/ui/reading-insights/memory"


class MemoryGrantInput(AnalysisGrantInput):
    purpose: Literal["reading_profile_inference"] = "reading_profile_inference"
    manifest_id: str | None = Field(default=None, max_length=64)


def save_grant(session: Session, data: MemoryGrantInput) -> Any:
    digest = None
    if data.scope_mode == "once":
        row = ReadingAnalysisManifestRepository(session).get(data.manifest_id or "")
        if row is None or row.purpose != MEMORY_PURPOSE:
            raise InvalidRequestError("本次授权需要先预览材料，并选择该清单")
        digest = row.manifest_hash
    return grant_analysis_authorization(
        session,
        scope_mode=data.scope_mode,
        material_classes=data.material_classes,
        settings=get_settings(),
        now=utc_now(),
        purpose=MEMORY_PURPOSE,
        manifest_hash=digest,
    )


def preview_memory(session: Session, data: MemoryManifestInput) -> dict[str, Any]:
    ensure_active(session, action="reading_insights.memory")
    now = utc_now()
    content = build_memory_manifest(session, kinds=data.kinds, exclusions=data.exclusions, now=now)
    with write_tx(session):
        row, _ = store_manifest(session, content, now=now)
        token = create_preview_token(session, manifest_id=row.id, purpose=MEMORY_PURPOSE, now=now)
    return {
        "manifest": content.model_dump(mode="json"),
        "manifest_id": row.id,
        "preview": token.model_dump(mode="json"),
    }


def submit_memory(request: Request, data: AnalysisSubmitInput, session: Session) -> dict[str, str]:
    from papersteward.jobs.ui_flow import FlowRequest, MemoryRunParams

    token = validate_preview_token(
        session, token=data.preview_token, purpose=MEMORY_PURPOSE, now=utc_now()
    )
    grant = active_grant_for_purpose(
        session, purpose=MEMORY_PURPOSE, manifest_hash=token.manifest_hash
    )
    if grant is None:
        raise NotAuthorizedError("研究记忆尚未授权，请核对端点与材料类别后授权")
    identity = data.run_id or uuid4().hex
    resolve_workspace(request).ui_flows.submit(
        FlowRequest(
            run_id=identity,
            kind="insight_memory",
            memory=MemoryRunParams(
                manifest_id=token.manifest_id,
                manifest_hash=token.manifest_hash,
                grant_id=grant.id,
            ),
        )
    )
    with write_tx(session):
        consume_preview_token(session, token, now=utc_now())
    return {"run_id": identity, "run_url": f"/ui/runs/{identity}"}


@router.post("/manifests", status_code=201)
def post_manifest(data: MemoryManifestInput, session: SessionDep) -> Any:
    with domain_errors():
        return preview_memory(session, data)


@router.post("/grants", status_code=201)
def post_grant(data: MemoryGrantInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        return save_grant(session, data)


@router.post("/runs", status_code=202)
def post_run(data: AnalysisSubmitInput, request: Request, session: SessionDep) -> Any:
    with domain_errors():
        return submit_memory(request, data, session)


@router.get("")
def get_memories(
    session: SessionDep, keyword: str = "", kind: str = "", start: str = "", end: str = ""
) -> Any:
    with domain_errors():
        return list_memory(session, keyword=keyword, kind=kind, start=start, end=end)


@router.get("/{memory_id}")
def get_memory_detail(memory_id: str, session: SessionDep) -> Any:
    with domain_errors():
        return get_memory(session, memory_id)


@router.get("/{memory_id}/revisions/{revision}")
def get_memory_revision(memory_id: str, revision: int, session: SessionDep) -> Any:
    with domain_errors():
        return get_memory(session, memory_id, revision)


@router.post("/{memory_id}/decisions", status_code=201)
def post_decision(memory_id: str, data: MemoryDecisionInput, session: SessionDep) -> Any:
    with domain_errors(), write_tx(session):
        return decide_memory(session, memory_id, data, now=utc_now())


@router.delete("/{memory_id}", status_code=204)
def remove_memory(memory_id: str, session: SessionDep) -> Response:
    with domain_errors(), write_tx(session):
        delete_memory(session, memory_id)
    return Response(status_code=204)


def _checkboxes(draft: dict[str, str], *, grant: bool = False) -> str:
    prefix = "cls_" if grant else "kind_"
    default = {"archive_metadata", "user_rating"}
    return "".join(
        f'<label class="check"><input type="checkbox" name="{prefix}{kind}" value="1"'
        + (" checked" if draft.get(prefix + kind) or (not draft and kind in default) else "")
        + f">{escape(label)} — {escape(MATERIAL_KIND_LIMITS[kind])}</label>"
        for kind, label in MATERIAL_KIND_LABELS.items()
    )


def _list_body(
    session: Session, *, keyword: str = "", asset_kind: str = "", start: str = "", end: str = ""
) -> str:
    memories = list_memory(session, keyword=keyword, start=start, end=end)
    body = (
        '<h2>研究记忆</h2><p class="notice">仅用于回顾与资产整理，'
        "不参与检索、排序、入队或研究档案。"
        "所有模型候选均待你核对；确认表示认可个人整理，不证明论文结论正确。</p>"
        '<section><h3>阅读事实</h3><p><a href="/ui/reading-insights?kind=all">全部历史事实</a>'
        ' · <a href="/ui/reading-insights/assets">知识与方法资产库</a></p></section>'
        '<form method="get" class="stacked-form"><label>问题或关键词'
        f'<input name="keyword" maxlength="200" value="{escape(keyword)}"></label>'
        '<label>资产类型<select name="asset_kind"><option value="">全部</option>'
        + "".join(
            f'<option value="{kind}"{" selected" if asset_kind == kind else ""}>{label}</option>'
            for kind, label in (
                ("harvest", "收获"),
                ("problem", "问题"),
                ("method", "方法卡"),
                ("evidence", "证据组"),
            )
        )
        + "</select></label>"
        f'<label>反馈或资产记录始日<input type="date" name="start" value="{escape(start)}"></label>'
        f'<label>截止日<input type="date" name="end" value="{escape(end)}"></label>'
        "<button>查找证据</button></form>"
    )
    for kind, title in (("value_standard", "长期价值标准候选"), ("interest", "近期兴趣与任务线索")):
        body += f"<section><h3>{title}</h3><ul>"
        selected = [item for item in memories if item.kind == kind]
        for item in selected:
            body += (
                f'<li><a href="{ROOT}/{item.memory_id}">{escape(item.statement)}</a> · '
                f"{STATUS_LABELS[item.effective_status]} · {item.group.distinct_papers} 篇"
                + (" · 仅当前任务适用" if item.scope == "task_only" else "")
                + "</li>"
            )
        if not selected:
            body += "<li>暂无条目；材料不足时不会补成长期偏好。</li>"
        body += "</ul></section>"
    assets = list_assets(
        session, kind=asset_kind or None, keyword=keyword or None, status=None, limit=200, offset=0
    )
    body += "<section><h3>知识资产与开放问题</h3><ul>"
    for item in assets["items"]:
        stamp = item["revision"].recorded_at.date().isoformat()
        if (start and stamp < start) or (end and stamp > end):
            continue
        body += (
            f'<li><a href="/ui/reading-insights/assets/{item["asset_id"]}">'
            f"{escape(item['revision'].title)}</a> · {stamp}</li>"
        )
    body += (
        "</ul><p>资产保持在同一资产库；近期没有新反馈不会删除旧资产。"
        "此处最多显示 200 条，更多结果可到资产库分页查找。</p></section>"
    )
    return body


def _analysis_forms(session: Session, draft: dict[str, str], preview: dict[str, Any] | None) -> str:
    status = analysis_authorization_status(session, settings=get_settings(), purpose=MEMORY_PURPOSE)
    body = (
        "<section><h3>整理新的研究记忆</h3><p>先按全部历史原始反馈分组，再请模型提出措辞。"
        "稳定候选至少 3 篇、可核查反馈跨 2 个自然月；集中补录、回溯声明及单任务样本会降低强度。"
        "对比近期 8 周、此前 8 周与全部历史。旧画像和报告摘要不会增加证据数量。</p>"
        f"<p>模型端点：{escape(status.llm_endpoint_identity or '尚未连接')}；"
        f"模型：{escape(status.llm_model or '未知')}</p>"
        f'<form method="post" action="{ROOT}/preview" class="stacked-form">'
        "<fieldset><legend>允许纳入的材料类别</legend>"
        + _checkboxes(draft)
        + "</fieldset><label>排除论文（每行一个论文 ID）"
        f'<textarea name="exclusions">{escape(draft.get("exclusions", ""))}</textarea></label>'
        "<button>预览材料（不发送）</button></form>"
    )
    manifest_id = ""
    if preview:
        manifest_id = str(preview["manifest_id"])
        manifest = preview["manifest"]
        coverage = manifest["coverage"]
        body += (
            "<section><h4>本次材料清单</h4>"
            f"<p>归档 {coverage['archived_total']} 篇，分析覆盖 {coverage['selected_papers']} 篇；"
            f"省略论文 {coverage['omitted_papers']} 篇、材料 {coverage['omitted_materials']} 条。"
            "单次最多 16 组、40 万输入字符；超出预算会明确停止或披露省略。</p><ul>"
        )
        for ref in manifest["materials"]:
            body += (
                f"<li>{escape(MATERIAL_KIND_LABELS[ref['kind']])} · {escape(ref['target_id'])} · "
                f"{escape(ref['version_key'])} · 校验 {escape(ref['ref_hash'])}</li>"
            )
        body += "</ul></section>"
        body += (
            f'<form method="post" action="{ROOT}/runs">'
            + hidden("preview_token", preview["preview"]["token"])
            + "<button>按此清单开始分析</button></form>"
        )
    body += (
        f'<form method="post" action="{ROOT}/grants" class="stacked-form">'
        "<fieldset><legend>研究记忆的独立用途授权</legend>"
    )
    body += _checkboxes(draft, grant=True)
    body += hidden("manifest_id", manifest_id)
    if preview:
        body += hidden("preview_token", preview["preview"]["token"])
    body += (
        '<label>授权范围<select name="scope_mode">'
        '<option value="workspace">当前工作区持续授权</option>'
        '<option value="once">仅本次清单（需先预览）</option></select></label>'
        '<label class="check"><input type="checkbox" name="acknowledged" value="1" required>'
        "我已核对上述端点、材料类别和研究记忆用途，选择将这些材料发送给该模型</label>"
        "<button>保存用途授权</button></fieldset></form>"
    )
    for grant in status.grants:
        labels = "、".join(MATERIAL_KIND_LABELS.get(kind, kind) for kind in grant.material_classes)
        body += (
            f"<p>授权 {escape(grant.grant_id)} · {'已撤回' if grant.revoked_at else '未撤回'}；"
            f"类别：{escape(labels)}</p>"
        )
        if grant.revoked_at is None:
            body += (
                f'<form method="post" action="{ROOT}/grants/{grant.grant_id}/revoke">'
                '<button class="secondary">撤回并停止后续使用</button></form>'
            )
    return body + "</section>"


@pages.get("")
def memory_page(
    session: SessionDep, keyword: str = "", asset_kind: str = "", start: str = "", end: str = ""
) -> Response:
    if get_onboarding(session).stage.value != "active":
        return RedirectResponse("/ui/setup", status_code=303)
    return _page(
        "研究记忆",
        _list_body(session, keyword=keyword, asset_kind=asset_kind, start=start, end=end)
        + _analysis_forms(session, {}, None),
    )


@pages.post("/preview")
async def preview_page(request: Request, session: SessionDep) -> Response:
    draft: dict[str, str] = {}
    try:
        draft = await fields(request)
        data = MemoryManifestInput.model_validate(
            {
                "kinds": [kind for kind in MATERIAL_KIND_LABELS if draft.get("kind_" + kind)],
                "exclusions": [
                    MaterialExclusion(paper_id=paper_id.strip(), reason="用户排除")
                    for paper_id in draft.get("exclusions", "").splitlines()
                    if paper_id.strip()
                ],
            }
        )
        preview = preview_memory(session, data)
        return _page("研究记忆材料预览", _analysis_forms(session, draft, preview))
    except PageError as exc:
        session.rollback()
        response = _page(
            "请检查研究记忆选材",
            f'<p role="alert">{escape(json_details(exc))}</p>'
            + _analysis_forms(session, draft, None),
        )
        response.status_code = getattr(exc, "status_code", 422)
        return response


@pages.post("/grants")
async def grant_page(request: Request, session: SessionDep) -> Response:
    try:
        form = await fields(request)
        data = MemoryGrantInput.model_validate(
            {
                "purpose": MEMORY_PURPOSE,
                "scope_mode": form.get("scope_mode"),
                "material_classes": [
                    kind for kind in MATERIAL_KIND_LABELS if form.get("cls_" + kind)
                ],
                "acknowledged": form.get("acknowledged") == "1",
                "manifest_id": form.get("manifest_id") or None,
            }
        )
        with write_tx(session):
            save_grant(session, data)
        if form.get("preview_token"):
            token = validate_preview_token(
                session, token=form["preview_token"], purpose=MEMORY_PURPOSE, now=utc_now()
            )
            manifest = ReadingAnalysisManifestRepository(session).get(token.manifest_id)
            if manifest is None:
                raise NotFoundError("材料清单不存在，请重新预览")
            preview = {
                "manifest_id": manifest.id,
                "manifest": manifest.content_json,
                "preview": {"token": token.token},
            }
            return _page(
                "研究记忆已授权",
                "<p>已保存此用途授权，可按刚才的清单开始分析。</p>"
                + _analysis_forms(session, form, preview),
            )
        return RedirectResponse(ROOT, status_code=303)
    except PageError as exc:
        return failure(exc, back=ROOT)


@pages.post("/grants/{grant_id}/revoke")
async def revoke_page(grant_id: str, request: Request, session: SessionDep) -> Response:
    try:
        await fields(request)
        with write_tx(session):
            revoke_analysis_authorization(session, grant_id=grant_id, now=utc_now())
        return RedirectResponse(ROOT, status_code=303)
    except PageError as exc:
        return failure(exc, back=ROOT)


@pages.post("/runs")
async def run_page(request: Request, session: SessionDep) -> Response:
    try:
        form = await fields(request)
        result = submit_memory(
            request, AnalysisSubmitInput(preview_token=form.get("preview_token", "")), session
        )
        return RedirectResponse(result["run_url"], status_code=303)
    except PageError as exc:
        return conflict_redirect(exc) or failure(exc, back=ROOT)


def _detail_body(
    session: Session,
    item: MemoryRevisionView,
    *,
    draft: dict[str, str] | None = None,
    historical: bool = False,
) -> str:
    group = item.group
    body = (
        f"<h2>{escape(item.statement)}</h2><p>{STATUS_LABELS[item.effective_status]}"
        f" · 版本 {item.revision}"
        f" · {'仅当前任务适用' if item.scope == 'task_only' else '仅用于个人回顾'}</p>"
        '<p class="notice">不影响检索或推荐；确认不证明论文结论正确。'
        "用户修订会保留，模型不会覆盖。</p>"
        f"<p>任务语境：{escape(group.context)}；独立文献 {group.distinct_papers} 篇；"
        f"反馈自然月 {escape('、'.join(group.feedback_months) or '未知')}。</p>"
        f"<p>首次反馈 {escape(str(group.first_feedback_at or '未知'))}；"
        f"最近反馈 {escape(str(group.last_feedback_at or '未知'))}。</p>"
        f"<p>近期 8 周 {group.windows['recent_8_weeks']} 篇；"
        f"此前 8 周 {group.windows['previous_8_weeks']} 篇；全部历史 {group.distinct_papers} 篇；"
        f"反馈时间未知 {group.windows['feedback_time_unknown']} 篇。"
        "窗口以本次清单生成时刻为截止。</p>"
        f"<p>未覆盖信息／限制：{escape('；'.join(group.limitations))}</p>"
        f"<p>{escape(item.explanation or '')}</p>"
    )
    for problem in item.source_problems:
        body += f'<p role="alert">{escape(problem)}</p>'
    for evidence in group.evidence:
        ref = evidence.ref
        role = {
            "supporting": "支撑",
            "contrary": "相反或限制",
            "context": "语境补充（不增加独立样本）",
        }[evidence.role]
        resolved = resolve_material_content(session, ref, built_at=item.recorded_at)
        body += (
            f"<details><summary>{role} · {escape(ref.target_id)} · "
            f"{escape(ref.version_key)}</summary>"
            f"<p>阅读日期：{escape(evidence.reading_start or '未知')}"
            f"（{escape(evidence.reading_basis)}）；"
            f"回溯声明／记录时间：{escape(str(evidence.reading_recorded_at or '未知'))}；"
            f"反馈形成时间：{escape(str(evidence.feedback_at or '未知'))}；"
            f"系统归档／补录：{escape(str(evidence.archived_at))}。</p>"
            + (
                f'<p><a href="/ui/papers/{quote(ref.paper_id, safe="")}">'
                "查看原始阅读与反馈版本</a></p>"
                if ref.paper_id
                else ""
            )
            + "<pre>"
            + escape(resolved.text if resolved.ok else "来源已移除或校验失败，未替换成最新内容")
            + "</pre></details>"
        )
    revisions = ReadingMemoryRepository(session).revisions(item.memory_id)
    body += (
        "<p>历史版本："
        + " · ".join(
            f'<a href="{ROOT}/{item.memory_id}/revisions/{row.revision}">{row.revision}</a>'
            for row in revisions
        )
        + "</p>"
    )
    if not historical:
        draft = draft or {}
        body += (
            f'<form method="post" action="{ROOT}/{item.memory_id}/decisions" class="stacked-form">'
            + hidden("expected_revision", item.revision)
            + hidden("idempotency_key", draft.get("idempotency_key") or uuid4().hex)
            + '<label>我的措辞<textarea name="statement" maxlength="2000" required>'
            + escape(draft.get("statement", item.statement))
            + "</textarea></label><label>适用说明或纠正原因"
            '<textarea name="explanation" maxlength="1000">'
            + escape(draft.get("explanation", item.explanation or ""))
            + "</textarea></label>"
            + '<label>决定<select name="action">'
            + "".join(
                f'<option value="{action}">{label}</option>'
                for action, label in (
                    ("confirm", "确认"),
                    ("revise", "仅修订措辞"),
                    ("task_only", "仅本次任务适用"),
                    ("not_preference", "这不代表偏好"),
                    ("defer", "暂不采用"),
                    ("disable", "停用"),
                    ("review", "标为需复核"),
                )
            )
            + "</select></label><button>保存我的决定</button></form>"
            + f'<p><a href="{ROOT}/{item.memory_id}/export">导出此条的个人版本与证据</a></p>'
            + f'<form method="post" action="{ROOT}/{item.memory_id}/delete">'
            "<p>删除将清除该条全部版本和本地画像派生登记；原始反馈、资产及已下载副本保留。</p>"
            '<label><input type="checkbox" name="confirmed" value="1" required>'
            "删除全部本地版本</label><button>删除研究记忆</button></form>"
        )
    return body


@pages.get("/{memory_id}")
def detail_page(memory_id: str, session: SessionDep) -> Response:
    try:
        return _page("核对研究记忆", _detail_body(session, get_memory(session, memory_id)))
    except PageError as exc:
        return failure(exc, back=ROOT)


@pages.get("/{memory_id}/revisions/{revision}")
def revision_page(memory_id: str, revision: int, session: SessionDep) -> Response:
    try:
        return _page(
            "研究记忆历史版本",
            _detail_body(session, get_memory(session, memory_id, revision), historical=True),
        )
    except PageError as exc:
        return failure(exc, back=ROOT)


@pages.post("/{memory_id}/decisions")
async def decision_page(memory_id: str, request: Request, session: SessionDep) -> Response:
    draft: dict[str, str] = {}
    try:
        draft = await fields(request)
        data = MemoryDecisionInput.model_validate(
            {**draft, "expected_revision": int(draft.get("expected_revision", "0"))}
        )
        with write_tx(session):
            decide_memory(session, memory_id, data, now=utc_now())
        return RedirectResponse(f"{ROOT}/{memory_id}", status_code=303)
    except PageError as exc:
        session.rollback()
        try:
            body = _detail_body(session, get_memory(session, memory_id), draft=draft)
        except NotFoundError:
            return failure(exc, back=ROOT)
        response = _page(
            "请核对后重试",
            f'<p role="alert">{escape(json_details(exc))}；你的输入已保留。</p>' + body,
        )
        response.status_code = getattr(exc, "status_code", 422)
        return response


@pages.post("/{memory_id}/delete")
async def delete_page(memory_id: str, request: Request, session: SessionDep) -> Response:
    try:
        form = await fields(request)
        if form.get("confirmed") != "1":
            raise InvalidRequestError("请核对删除范围并勾选确认")
        with write_tx(session):
            delete_memory(session, memory_id)
        return RedirectResponse(ROOT, status_code=303)
    except PageError as exc:
        return failure(exc, back=ROOT)


@pages.get("/{memory_id}/export")
def export_page(memory_id: str, session: SessionDep, confirm_private: str = "") -> Response:
    try:
        item = get_memory(session, memory_id)
        if confirm_private != "yes":
            return _page(
                "导出个人研究记忆",
                "<p>此副本包含所有画像措辞、私人推测、用户修订、版本化来源与决定；默认不进入事实分享副本。</p>"
                + '<form method="get">'
                + hidden("confirm_private", "yes")
                + "<button>我选择下载此条的个人完整副本</button></form>",
            )
        rows = ReadingMemoryRepository(session).revisions(item.memory_id)
        content = {
            "contract": "reading-memory.personal-copy/1",
            "revisions": [memory_revision_view(session, row) for row in rows],
        }
        return Response(
            json.dumps(jsonable_encoder(content), ensure_ascii=False),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="reading-memory.json"',
                "Cache-Control": "no-store",
            },
        )
    except PageError as exc:
        return failure(exc, back=ROOT)
