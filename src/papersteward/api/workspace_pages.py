"""工作区管理页：浏览器内切换、创建与删除工作区。

- 切换经 cookie ``ps_workspace`` 携带身份，请求级解析见 ``workspace_runtime``；
- 创建后自动切换并进入首次设置；可选复制当前工作区的模型连接；
- 删除照搬「重置工作区」的预览-确认范式，整目录移入 backups/ 可恢复；
- 顶栏下拉是 htmx 懒加载片段，遵守 fragment 契约（稳定容器 id + 双条件判定）。
"""

from __future__ import annotations

from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import PageError, failure, fields, hidden, json_details
from papersteward.api.html import HTML_HEADERS as HEADERS
from papersteward.api.html import fragment_error
from papersteward.api.html import page as _page
from papersteward.api.workspace_runtime import (
    WorkspaceRuntimeHub,
    resolve_workspace,
    set_workspace_cookie,
)
from papersteward.core.entities import utc_now
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.llm.runtime import CONFIG_KEY
from papersteward.storage import session_scope
from papersteward.storage.repositories import ConfigRepository, RunRepository
from papersteward.storage.workspace import workspace_counts
from papersteward.storage.workspace_registry import (
    DEFAULT_WORKSPACE_ID,
    WorkspaceInfo,
)

router = APIRouter(prefix="/ui", include_in_schema=False)

_RUNNING_BADGE = '<span class="ws-badge">运行中</span>'
_UNAVAILABLE_BADGE = '<span class="ws-badge warn">暂不可用</span>'


def _running_badge(session: Session) -> str:
    return _RUNNING_BADGE if bool(RunRepository(session).running_ui_runs()) else ""


def _is_fragment(request: Request) -> bool:
    """裸片段防护：fragment=1 查询参数与 hx-request 头须同时成立（pages.py 同款）。"""
    return (
        request.query_params.get("fragment") == "1" and request.headers.get("hx-request") == "true"
    )


# —— 顶栏切换器片段 ——————————————————————————————————————————————


def _switcher_fragment(infos: list[WorkspaceInfo], current_id: str, running: bool) -> str:
    current = next(info for info in infos if info.id == current_id)
    items = []
    for info in infos:
        is_current = info.id == current_id
        badge = _RUNNING_BADGE if running and is_current else ""
        mark = '<span class="ws-mark" aria-hidden="true">✓ </span>' if is_current else ""
        aria = ' aria-current="true"' if is_current else ""
        items.append(
            '<form method="post" action="/ui/workspaces/switch">'
            f"{hidden('workspace_id', info.id)}"
            f'<button type="submit"{aria} title="切换到「{escape(info.name)}」">'
            f"{mark}{escape(info.name)}{badge}</button></form>"
        )
    return (
        '<details class="nav-group" id="workspace-switcher">'
        f'<summary><span class="ws-name">{escape(current.name)}</span></summary>'
        f'<div class="nav-menu">{"".join(items)}'
        '<a href="/ui/workspaces">管理工作区…</a></div></details>'
    )


@router.get("/workspaces/switcher")
def workspace_switcher(request: Request, session: SessionDep, fragment: str = "") -> Response:
    if not _is_fragment(request):
        return RedirectResponse("/ui/workspaces", status_code=303)
    try:
        hub: WorkspaceRuntimeHub = request.app.state.workspaces
        current = resolve_workspace(request)
        return HTMLResponse(
            _switcher_fragment(hub.workspaces(), current.info.id, bool(_running_badge(session))),
            headers=HEADERS,
        )
    except PageError as exc:
        return fragment_error("workspace-switcher", json_details(exc))


# —— 管理页 ————————————————————————————————————————————————


def _workspace_row(
    session: Session, hub: WorkspaceRuntimeHub, info: WorkspaceInfo, current_id: str
) -> str:
    is_current = info.id == current_id
    if info.id == DEFAULT_WORKSPACE_ID:
        meta = "初始工作区，数据在主数据目录"
    else:
        meta = f"创建于 {escape(info.created_at[:10])}" if info.created_at else ""
    if is_current:
        badge = _running_badge(session)
    else:
        try:
            runtime = hub.open(info.id)
        except PageError:
            badge = _UNAVAILABLE_BADGE
        else:
            with session_scope(runtime.session_factory) as ws_session:
                badge = _running_badge(ws_session)
    badge_html = f" {badge}" if badge else ""
    if is_current:
        actions = '<a class="button secondary" href="/ui/settings/reset">重置工作区</a>'
    else:
        actions = (
            f'<form class="inline" method="post" action="/ui/workspaces/switch">'
            f'{hidden("workspace_id", info.id)}<button class="secondary">切换</button></form>'
            f'<a class="button secondary" href="/ui/workspaces/{quote(info.id, safe="")}/delete">'
            "删除…</a>"
        )
    current_mark = '<span class="muted">当前使用</span>' if is_current else ""
    return (
        '<li><div class="ws-meta"><h3>'
        + ("✓ " if is_current else "")
        + f'{escape(info.name)}</h3><p class="muted">{meta}{badge_html}</p></div>'
        f'<div class="actions">{current_mark}{actions}</div></li>'
    )


def _manage_body(
    session: Session,
    hub: WorkspaceRuntimeHub,
    current_id: str,
    *,
    deleted: bool = False,
    error: str = "",
    name_value: str = "",
) -> str:
    body = ""
    if deleted:
        body += (
            '<p class="notice success">工作区已删除。整个目录备份保存在数据目录的 '
            "backups/ 下，需要时可手动恢复。</p>"
        )
    if error:
        body += f'<p role="alert">{escape(error)}</p>'
    rows = "".join(_workspace_row(session, hub, info, current_id) for info in hub.workspaces())
    body += f'<article><h2>全部工作区</h2><ul class="ws-list">{rows}</ul></article>'
    name_attr = f' value="{escape(name_value)}"' if name_value else ""
    placeholder = "例如：博弈论综述"
    body += (
        "<article><h2>新建工作区</h2>"
        '<form method="post" action="/ui/workspaces" class="stacked-form">'
        '<label>工作区名称<input name="name" required maxlength="40" '
        f'placeholder="{placeholder}"{name_attr}></label>'
        '<label class="check"><input type="checkbox" name="copy_model" value="yes" checked>'
        "复制当前工作区的模型连接（API 地址、密钥与超时设置）</label>"
        '<button type="submit">创建并切换</button></form>'
        '<p class="muted">每个工作区有独立的论文库、研究档案与阅读队列，'
        "新工作区创建后会进入首次设置。</p></article>"
    )
    return body


def _manage_page(
    request: Request,
    session: Session,
    *,
    deleted: bool = False,
    error: str = "",
    name_value: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    hub: WorkspaceRuntimeHub = request.app.state.workspaces
    current = resolve_workspace(request)
    rendered = _page(
        "工作区管理",
        _manage_body(
            session,
            hub,
            current.info.id,
            deleted=deleted,
            error=error,
            name_value=name_value,
        ),
        nav="工作区管理",
    )
    rendered.status_code = status_code
    return rendered


@router.get("/workspaces")
def manage_workspaces(request: Request, session: SessionDep, deleted: bool = False) -> HTMLResponse:
    try:
        return _manage_page(request, session, deleted=deleted)
    except PageError as exc:
        return failure(exc, back="/ui/today")


# —— 创建与切换 ——————————————————————————————————————————————


def _copy_model_connection(session: Session, hub: WorkspaceRuntimeHub, workspace_id: str) -> None:
    """把当前工作区已保存的模型连接写入新工作区；目标已有配置时不动。"""
    source = ConfigRepository(session).latest(CONFIG_KEY)
    if source is None:
        return
    target_runtime = hub.open(workspace_id)
    with session_scope(target_runtime.session_factory) as ws_session:
        if ConfigRepository(ws_session).latest(CONFIG_KEY) is None:
            ConfigRepository(ws_session).add(source)


@router.post("/workspaces")
async def create_workspace(request: Request, session: SessionDep) -> Response:
    hub: WorkspaceRuntimeHub = request.app.state.workspaces
    data: dict[str, str] = {}
    try:
        data = await fields(request)
        info = hub.create_workspace(data.get("name", ""), now=utc_now())
    except PageError as exc:
        if isinstance(exc, InvariantViolation):
            # 跨站守卫保持 409 契约（与重置工作区一致），不走表单重渲染。
            return failure(exc, back="/ui/workspaces")
        # X-01：保留已填名称原地重渲染，状态码标 400。
        return _manage_page(
            request,
            session,
            error=json_details(exc),
            name_value=data.get("name", ""),
            status_code=400,
        )
    copy_failed = ""
    if data.get("copy_model") == "yes":
        try:
            _copy_model_connection(session, hub, info.id)
        except PageError:
            copy_failed = "&copy_failed=1"
    response = RedirectResponse(f"/ui/setup?created=1{copy_failed}", status_code=303)
    set_workspace_cookie(response, info.id)
    return response


@router.post("/workspaces/switch")
async def switch_workspace(request: Request) -> Response:
    hub: WorkspaceRuntimeHub = request.app.state.workspaces
    try:
        data = await fields(request)
        info = hub.info(data.get("workspace_id", ""))
        # 迁移等错误在落 cookie 前暴露，避免切进一个不可用的工作区。
        hub.open(info.id)
        response = RedirectResponse("/ui/today", status_code=303)
        set_workspace_cookie(response, info.id)
        return response
    except PageError as exc:
        return failure(exc, back="/ui/workspaces")


# —— 删除 —————————————————————————————————————————————————


@router.get("/workspaces/{workspace_id}/delete")
def delete_workspace_preview(request: Request, workspace_id: str) -> HTMLResponse:
    try:
        hub: WorkspaceRuntimeHub = request.app.state.workspaces
        if workspace_id == DEFAULT_WORKSPACE_ID:
            raise InvalidRequestError("默认工作区不能删除；如需清空数据请用设置页的「重置工作区」")
        info = hub.info(workspace_id)
        current = resolve_workspace(request)
        runtime = hub.open(workspace_id)
        with session_scope(runtime.session_factory) as ws_session:
            counts = workspace_counts(ws_session)
            busy = bool(RunRepository(ws_session).running_ui_runs())
        body = (
            "<article><h2>删除工作区</h2><p>将删除工作区「"
            f"{escape(info.name)}"
            "」的全部数据：论文、阅读反馈、归档、研究档案、分类标签与运行记录。"
            "删除前会把整个工作区目录移动到数据目录的 backups/ 下（deleted- 前缀），"
            "需要时可手动移回恢复。</p>"
            f"<p>其中有 {counts.get('paper', 0)} 篇论文、{counts.get('rating', 0)} 条阅读反馈、"
            f"{counts.get('intake_material', 0)} 份研究材料。</p>"
        )
        if current.info.id == workspace_id:
            body += '<p class="muted">这是当前正在使用的工作区，删除后会回到默认工作区。</p>'
        if busy:
            body += '<p class="notice">有任务正在运行，完成后才能删除工作区。</p>'
        disabled = " disabled" if busy else ""
        body += (
            '<form method="post" action="'
            f"/ui/workspaces/{quote(workspace_id, safe='')}/delete"
            '"><label>输入“删除工作区”确认删除<input name="confirmation" required '
            'autocomplete="off" placeholder="删除工作区"></label>'
            f'<button class="danger"{disabled}>删除工作区</button></form>'
            '<a href="/ui/workspaces">取消，返回工作区管理</a></article>'
        )
        return _page("删除工作区", body)
    except PageError as exc:
        return failure(exc, back="/ui/workspaces")


@router.post("/workspaces/{workspace_id}/delete")
async def delete_workspace(request: Request, workspace_id: str) -> Response:
    hub: WorkspaceRuntimeHub = request.app.state.workspaces
    try:
        data = await fields(request)
        if data.get("confirmation") != "删除工作区":
            raise InvalidRequestError("请输入“删除工作区”确认删除")
        current = resolve_workspace(request)
        runtime = hub.open(workspace_id)
        with runtime.ui_flows.lock:
            with session_scope(runtime.session_factory) as ws_session:
                if RunRepository(ws_session).running_ui_runs():
                    raise ConflictError("有任务正在运行，完成后才能删除工作区")
            hub.remove_workspace(workspace_id, now=utc_now())
        response = RedirectResponse("/ui/workspaces?deleted=1", status_code=303)
        if current.info.id == workspace_id:
            # 删掉的是当前工作区：身份落回默认，避免悬空 cookie。
            set_workspace_cookie(response, DEFAULT_WORKSPACE_ID)
        return response
    except PageError as exc:
        return failure(exc, back="/ui/workspaces")


__all__ = ["router"]
