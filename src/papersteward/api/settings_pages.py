"""模型连接设置页：表单校验后调用 /settings API 同一服务层。"""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from papersteward.api.deps import SessionDep
from papersteward.api.pages import _guard, _page
from papersteward.config import get_settings
from papersteward.errors import AppError
from papersteward.llm.runtime import (
    LLMSettingsPayload,
    resolve_llm_config,
    save_llm_config,
)

from .form_utils import failure, fields, input_field

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
        + input_field("timeout_s", str(int(config_timeout)), "请求超时（秒）")
    )
    consent = (
        '<label class="check"><input type="checkbox" name="consent" value="yes" required>'
        "我知道论文的标题与摘要会发送给上面配置的模型服务，仅用于筛选和概括</label>"
    )
    actions = (
        '<div class="actions"><button type="button" data-test-llm data-form="llm-settings">'
        '测试连接</button><button type="submit">保存并继续</button>'
        '<span id="llm-test-result" role="status"></span></div>'
    )
    heading = "" if compact else "<h2>连接模型</h2>"
    return (
        f'{heading}<form id="llm-settings" method="post" action="/ui/settings/llm">'
        f'<input type="hidden" name="next" value="{escape(next_url)}">{body}'
        f"{consent}{actions}</form>"
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
def settings_page(session: SessionDep) -> HTMLResponse:
    config = resolve_llm_config(session, get_settings())
    body = "<h1>模型连接</h1>"
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
    return _page("模型连接", body)


@router.post("/settings/llm")
async def save_settings(request: Request, session: SessionDep) -> Response:
    try:
        _guard(request)
        data = await fields(request)
        payload = LLMSettingsPayload.model_validate(
            {
                "base_url": data.get("base_url", ""),
                "api_key": data.get("api_key", ""),
                "model": data.get("model", ""),
                "timeout_s": data.get("timeout_s", "60"),
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
        return failure(exc, back="/ui/settings")


__all__ = ["router", "llm_form"]
