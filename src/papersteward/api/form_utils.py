"""页面表单的共享工具：读取、转义、输入组件与错误页。"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from papersteward.api.pages import _guard, _page
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, ConflictError, InvalidRequestError


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


def conflict_redirect(exc: Exception) -> RedirectResponse | None:
    """已有任务在运行时跳到该任务的进度页，而不是停在错误页。"""
    if isinstance(exc, ConflictError) and isinstance(exc.details, dict):
        run_id = exc.details.get("run_id")
        if run_id:
            return RedirectResponse(f"/ui/runs/{quote(str(run_id), safe='')}", status_code=303)
    return None


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


def json_details(exc: Exception) -> str:
    """把校验异常压缩成可读文本，供错误页展示。"""
    if isinstance(exc, ValidationError):
        return "；".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()
        )
    message = getattr(exc, "message", None)
    return str(message) if message else str(exc)


def safe_str(value: Any) -> str:
    return "" if value is None else str(value)


__all__ = [
    "conflict_redirect",
    "failure",
    "fields",
    "form",
    "hidden",
    "input_field",
    "json_details",
    "safe_str",
    "textarea",
]
