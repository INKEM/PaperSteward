"""模型连接设置页：表单校验后调用 /settings API 同一服务层。"""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.api.html import guard as _guard
from papersteward.api.html import page as _page
from papersteward.config import DEFAULT_LLM_TIMEOUT_S, get_settings
from papersteward.errors import AppError
from papersteward.llm.runtime import (
    LLMSettingsPayload,
    resolve_llm_config,
    save_llm_config,
)

from .form_utils import failure, fields, input_field
from .workspace_runtime import resolve_workspace

router = APIRouter(prefix="/ui", include_in_schema=False)


def llm_form(
    config_base_url: str,
    config_model: str,
    config_timeout: float,
    *,
    compact: bool = False,
    next_url: str = "/ui/settings",
    key_saved: bool = False,
    key_from_env: bool = False,
) -> str:
    """连接模型表单；设置页与首次设置向导共用。"""
    if key_from_env:
        placeholder = "沿用环境变量中的密钥，可留空"
    elif key_saved:
        placeholder = "已保存，留空则沿用"
    else:
        placeholder = "非本机地址必填；本机模型可留空"
    body = (
        input_field(
            "base_url",
            config_base_url,
            "API 地址（OpenAI 兼容，如 https://api.deepseek.com/v1）",
        )
        + '<label>API 密钥<input type="password" name="api_key" placeholder="'
        + placeholder
        + '" autocomplete="off"></label>'
        + input_field("model", config_model, "模型名称（如 deepseek-chat、gpt-4o-mini）")
        # UXR-11/§7.2：超时属于低频配置，收进高级设置。
        + '<details class="advanced"><summary>高级设置（请求超时）</summary>'
        + '<label>请求超时（秒，模型响应慢可调大）<input type="number" name="timeout_s" '
        f'value="{int(config_timeout)}" min="5" max="600" step="1" required></label>' + "</details>"
    )
    consent = (
        '<label class="check"><input type="checkbox" name="consent" value="yes" required>'
        "我知道处理论文时会把标题、摘要及任务所需的研究方向和分类规则发送给上面配置的"
        "模型服务，用于筛选、概括和评分；生成草案还会发送研究材料、修改意见和上一版草案；"
        "阅读备注保存在本机</label>"
    )
    actions = (
        '<div class="actions"><button type="button" data-test-llm="llm-settings">'
        '测试连接</button><button type="submit">保存模型连接</button>'
        '<span id="llm-test-result" role="status"></span></div>'
    )
    heading = "" if compact else "<h2>连接模型</h2>"
    return (
        f'{heading}<form id="llm-settings" method="post" action="/ui/settings/llm" '
        'class="settings-form">'
        f'<input type="hidden" name="next" value="{escape(next_url)}">{body}'
        f"{consent}{actions}"
        # UXR-13：说明测试的取值来源，区分「保存配置」与「验证连接」。
        + '<p class="muted">「测试连接」用上方表单当前值发起一次真实请求（不保存）；'
        "密钥留空时沿用已保存的密钥。</p></form>"
    )


def _status_block(session: SessionDep) -> str:
    config = resolve_llm_config(session, get_settings())
    if config.source != "db" or not config.ready:
        return ""
    key_hint = ""
    if config.has_api_key:
        key_hint = "，密钥来自环境变量" if config.api_key_from_env else "，密钥已保存"
    return (
        f'<p class="notice">当前连接：{escape(config.model or "")} @ '
        f"{escape(config.base_url)}{key_hint}</p>"
    )


@router.get("/settings")
def settings_page(session: SessionDep, saved: bool = False) -> HTMLResponse:
    config = resolve_llm_config(session, get_settings())
    body = ""
    if saved:
        body += '<p class="notice success">模型连接已保存。可点「测试连接」验证是否可用。</p>'
    body += _status_block(session)
    body += llm_form(
        config.base_url,
        config.model or "",
        config.timeout_s,
        key_saved=config.has_api_key,
        key_from_env=config.api_key_from_env,
    )
    body += (
        "<p>配置保存在本机工作区数据库中，密钥不会上传。"
        '保存后即可在 <a href="/ui/setup">首次设置</a> 继续上传研究材料。</p>'
    )
    from papersteward.api.profile_view import confirmed_results

    body += confirmed_results(session)
    body += (
        "<article><h2>工作区管理</h2><p>想重新执行首次设置，可先备份并清空此工作区的数据。</p>"
        '<a class="button secondary" href="/ui/settings/reset">重置工作区（清空数据）</a></article>'
    )
    return _page("设置", body)


@router.post("/settings/llm")
async def save_settings(request: Request, session: SessionDep) -> Response:
    data: dict[str, str] = {}
    try:
        _guard(request)
        data = await fields(request)
        payload = LLMSettingsPayload.model_validate(
            {
                "base_url": data.get("base_url", ""),
                "api_key": data.get("api_key", ""),
                "model": data.get("model", ""),
                "timeout_s": data.get("timeout_s", str(int(DEFAULT_LLM_TIMEOUT_S))),
                "consent": data.get("consent") == "yes",
            }
        )
        with session.begin():
            save_llm_config(session, payload, get_settings())
        query = urlencode({"saved": "1"})
        requested = data.get("next") or "/ui/settings"
        next_url = (
            requested if requested.startswith("/ui/") and "//" not in requested else "/ui/settings"
        )
        separator = "&" if "?" in next_url else "?"
        return RedirectResponse(f"{next_url}{separator}{query}", status_code=303)
    except (AppError, ValidationError, ValueError) as exc:
        # X-01：保存失败时保留已填的地址/模型名/超时重渲染表单；密钥不回显。
        if isinstance(exc, ValidationError):
            message = "输入无效：" + "；".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()
            )
        else:
            message = str(getattr(exc, "message", exc))
        try:
            timeout_value = int(float(data.get("timeout_s", "") or DEFAULT_LLM_TIMEOUT_S))
        except ValueError:
            timeout_value = int(DEFAULT_LLM_TIMEOUT_S)
        form = llm_form(
            data.get("base_url", ""),
            data.get("model", ""),
            float(timeout_value),
            next_url=data.get("next") or "/ui/settings",
        )
        return _page(
            "设置",
            f'<p role="alert">{escape(message)}</p>'
            + form
            + '<p><a href="/ui/settings">返回设置</a></p>',
        )


__all__ = ["router", "llm_form"]


@router.get("/settings/reset")
def reset_preview(session: SessionDep) -> HTMLResponse:
    from papersteward.storage.repositories import RunRepository
    from papersteward.storage.workspace import workspace_counts

    counts = workspace_counts(session)
    busy = bool(RunRepository(session).running_ui_runs())
    body = (
        "<article><h2>重置工作区并重新开始</h2>"
        "<p>将清空论文、阅读反馈、归档、历史日报、运行记录、上传材料、研究档案和分类标签。"
        "重置前会自动保存本地数据库备份；已有导出文件与备份保留。</p>"
        f"<p>当前有 {counts.get('paper', 0)} 篇论文、{counts.get('rating', 0)} 条阅读反馈、"
        f"{counts.get('intake_material', 0)} 份研究材料。</p>"
    )
    if busy:
        body += '<p class="notice">有任务正在运行，完成后才能重置工作区。</p>'
    disabled = " disabled" if busy else ""
    body += (
        '<form method="post" action="/ui/settings/reset">'
        '<label class="check"><input type="checkbox" name="keep_model" value="yes" checked>'
        "保留当前模型连接，方便重新执行首次设置（取消勾选会清空已保存的模型配置）</label>"
        '<label>输入“重置工作区”确认清空<input name="confirmation" required '
        'autocomplete="off" placeholder="重置工作区"></label>'
        f'<button class="danger"{disabled}>清空数据并重新开始</button></form>'
        '<a href="/ui/settings">取消，返回设置</a></article>'
    )
    return _page("重置工作区", body, nav="设置")


@router.post("/settings/reset")
async def reset_workspace(request: Request, session: SessionDep) -> Response:
    from sqlalchemy import text

    from papersteward.core.entities import utc_now
    from papersteward.errors import ConflictError, InvalidRequestError
    from papersteward.storage.repositories import RunRepository
    from papersteward.storage.workspace import backup_workspace, clear_workspace

    try:
        data = await fields(request)
        if data.get("confirmation") != "重置工作区":
            raise InvalidRequestError("请输入“重置工作区”确认清空")
        runtime = resolve_workspace(request)
        with runtime.ui_flows.lock, session.begin():
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            if RunRepository(session).running_ui_runs():
                raise ConflictError("有任务正在运行，完成后才能重置工作区")
            backup_workspace(
                runtime.engine,
                runtime.settings.data_dir / "backups",
                now=utc_now(),
            )
            clear_workspace(session, keep_model=data.get("keep_model") == "yes")
        return RedirectResponse("/ui/setup?reset=1", status_code=303)
    except OSError:
        from papersteward.errors import InvalidRequestError

        return failure(
            InvalidRequestError("备份失败，原数据保留。请检查数据目录权限和磁盘空间后重试。"),
            back="/ui/settings/reset",
        )
    except (AppError, ValueError) as exc:
        return failure(exc, back="/ui/settings/reset")
