"""UI 页面外壳与共享组件（Q-01/Q-02）。

本模块是 api 层的叶子：只依赖服务层与 html 标准库，不 import 任何其他 api
模块；页面模块（pages/workflow_pages/settings_pages/setup_pages/form_utils）
统一从这里取外壳、守卫与通用片段，消除原先的相互导入环。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from papersteward import __version__ as _app_version
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.errors import AppError

# 资产版本号：随应用版本变化，配合长缓存策略让样式更新立即生效（V-11）。
_ASSET_VERSION = _app_version

HTML_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}

_NAV_LINKS = (
    ("/ui/queue", "阅读队列", "阅读队列"),
    ("/ui/shelves", "特殊桶", "特殊桶"),
    ("/ui/views", "排序视图", "排序视图"),
    ("/ui/archive", "归档", "归档"),
    ("/ui/setup", "首次设置", "首次设置"),
    ("/ui/settings", "设置", "设置"),
    ("/ui/help", "帮助", "使用帮助"),
)


def page(title: str, body: str, *, nav: str | None = None) -> HTMLResponse:
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
    active = nav or title
    navigation = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if active == key else "") + f">{label}</a>"
        for url, label, key in _NAV_LINKS
    )
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="htmx-config" content="{escape(json.dumps(config))}">'
        f"<title>{escape(title)} · PaperSteward</title>"
        f'<link rel="icon" type="image/svg+xml" href="/ui/static/favicon.svg">'
        f'<link rel="stylesheet" href="/ui/static/style.css?v={escape(_ASSET_VERSION)}">'
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
        headers=HTML_HEADERS,
    )


def guard(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
    ):
        raise InvariantViolation("ui.origin", "拒绝跨站操作")
    if not origin and request.headers.get("x-papersteward-ui") != "1":
        raise InvariantViolation("ui.origin", "需要同源页面操作")


def paper_link(paper_id: str, text: str) -> str:
    """论文详情链接；全站统一 /ui/papers/{id} 约定。"""
    return f'<a href="/ui/papers/{quote(paper_id, safe="")}">{text}</a>'


def select_options(options: Iterable[tuple[str, str]], selected: str | None = None) -> str:
    """<option> 列表；selected 匹配 value。"""
    return "".join(
        f'<option value="{escape(value)}"{" selected" if value == selected else ""}>'
        f"{escape(label)}</option>"
        for value, label in options
    )


def pagination(
    base_url: str,
    params: dict[str, Any],
    *,
    offset: int,
    total: int,
    page_size: int = 50,
    prev_label: str = "上一页",
    next_label: str = "下一页",
    next_only: bool = False,
) -> str:
    """分页链接；params 含筛选参数（不含 offset），自动拼接。"""
    links = ""
    if not next_only and offset:
        query = urlencode({**params, "offset": max(0, offset - page_size)})
        links += f'<a class="button secondary" href="{base_url}?{escape(query)}">{prev_label}</a> '
    if offset + page_size < total:
        query = urlencode({**params, "offset": offset + page_size})
        links += f'<a class="button secondary" href="{base_url}?{escape(query)}">{next_label}</a> '
    return links


def steps(
    items: Sequence[str], current: str | int | None = None, *, aria_label: str | None = None
) -> str:
    """步骤条；current 为序号（1 起）或步骤文本。"""
    body = ""
    for index, name in enumerate(items, 1):
        if current is not None and current in (index, name):
            body += f'<li class="current" aria-current="step">{escape(name)}</li>'
        else:
            body += f"<li>{escape(name)}</li>"
    label = f' aria-label="{escape(aria_label)}"' if aria_label else ""
    return f'<ol class="steps"{label}>{body}</ol>'


def success_note(message: str, *, fragment: bool = False) -> str:
    """成功提示；htmx 片段带 autofocus 让焦点落在结果上。"""
    if fragment:
        return f'<p class="success" tabindex="-1" autofocus>{escape(message)}</p>'
    return f'<p class="success">{escape(message)}</p>'


def _error_status(exc: DomainError | AppError | ValidationError) -> int:
    if isinstance(exc, ValidationError):
        return 422
    if isinstance(exc, AppError):
        return exc.status_code
    return 409


def error_fragment(
    exc: DomainError | AppError | ValidationError, *, back: str = "/ui/today"
) -> str:
    """卡片原地替换用的错误片段（htmx swap 目标）。"""
    message = (
        exc.message
        if isinstance(exc, DomainError | AppError)
        else "输入无效，请检查评分、方式或备注。"
    )
    return (
        f'<article><p role="alert" tabindex="-1" autofocus>{escape(message)}</p>'
        f'<a href="{escape(back)}">返回队列</a></article>'
    )


def error_response(
    exc: DomainError | AppError | ValidationError, *, back: str = "/ui/setup"
) -> HTMLResponse:
    """错误页：可配返回链接；校验错误附字段原因。"""
    if isinstance(exc, ValidationError):
        details = "；".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()
        )
        message = "输入无效：" + details
        status = 422
    elif isinstance(exc, DomainError | AppError):
        message = exc.message
        status = _error_status(exc)
    else:
        message, status = "输入格式无效，请检查 JSON 或表单后重试。", 422
    rendered = page(
        "请检查后重试",
        f'<p role="alert">{escape(message)}</p><a href="{escape(back)}">返回页面检查后重试</a>',
    )
    rendered.status_code = status
    return rendered


def fragment_error(container_id: str, message: str, *, status_code: int = 409) -> HTMLResponse:
    """htmx 片段请求的错误返回（UXR-02）：保留稳定容器 id，
    后续请求仍能以同一容器为目标；错误就近展示并接管焦点。"""
    return HTMLResponse(
        f'<div id="{escape(container_id)}">'
        f'<p role="alert" tabindex="-1" autofocus>{escape(message)}</p></div>',
        status_code=status_code,
        headers=HTML_HEADERS,
    )


__all__ = [
    "HTML_HEADERS",
    "error_fragment",
    "error_response",
    "fragment_error",
    "guard",
    "page",
    "paper_link",
    "pagination",
    "select_options",
    "steps",
    "success_note",
]
