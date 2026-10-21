"""FastAPI 统一错误响应。

把业务错误、请求参数校验错误、Starlette HTTP 错误与未处理异常统一为
``ErrorResponse`` 信封；未处理异常只返回通用文案，详细堆栈只进日志。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from papersteward.api.constants import REQUEST_ID_HEADER
from papersteward.errors import AppError, ErrorBody, ErrorCode, ErrorResponse
from papersteward.observability.logging import (
    get_request_id,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str | None:
    """优先读取中间件写入的 request.state，回退到 contextvar。"""
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    return get_request_id()


def _response(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    resolved_request_id = request_id or get_request_id()
    response_headers = dict(headers) if headers else {}
    if resolved_request_id:
        # 500 由最外层 ServerErrorMiddleware 处理，不会经过 RequestIdMiddleware，
        # 因此错误响应自身必须回写 X-Request-ID。
        response_headers[REQUEST_ID_HEADER] = resolved_request_id
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details,
            request_id=resolved_request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers or None,
    )


_DEFAULT_HTTP_MESSAGES = {
    HTTPStatus.BAD_REQUEST: "请求参数不合法",
    HTTPStatus.UNAUTHORIZED: "未认证",
    HTTPStatus.FORBIDDEN: "没有权限",
    HTTPStatus.NOT_FOUND: "请求的资源不存在",
    HTTPStatus.METHOD_NOT_ALLOWED: "请求方法不被允许",
    HTTPStatus.CONFLICT: "请求与当前状态冲突",
    HTTPStatus.UNPROCESSABLE_ENTITY: "请求参数校验失败",
    HTTPStatus.INTERNAL_SERVER_ERROR: "服务器内部错误",
}


def _http_message(status_code: int) -> str:
    try:
        status = HTTPStatus(status_code)
    except ValueError:
        return "请求失败"
    return _DEFAULT_HTTP_MESSAGES.get(status, "请求失败")


def _is_standard_detail(status_code: int, detail: str) -> bool:
    """判断 detail 是否只是框架默认的英文短语；是则换成中文提示。"""
    try:
        return detail == HTTPStatus(status_code).phrase
    except ValueError:
        return False


def _http_code(status_code: int) -> ErrorCode:
    if status_code == HTTPStatus.NOT_FOUND:
        return ErrorCode.NOT_FOUND
    if status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        return ErrorCode.METHOD_NOT_ALLOWED
    return ErrorCode.HTTP_ERROR


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理 AppError 及其子类。"""
    if not isinstance(exc, AppError):
        return await unhandled_exception_handler(request, exc)
    return _response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id=_request_id(request),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """保留 Starlette HTTPException 的状态码与 headers。"""
    if not isinstance(exc, StarletteHTTPException):
        return await unhandled_exception_handler(request, exc)
    detail = exc.detail
    if isinstance(detail, str):
        message = (
            _http_message(exc.status_code)
            if _is_standard_detail(exc.status_code, detail)
            else detail
        )
        details = None
    elif isinstance(detail, dict):
        details = detail
        message = str(detail.get("message") or _http_message(exc.status_code))
    else:
        message = _http_message(exc.status_code)
        details = {"detail": detail} if detail is not None else None
    return _response(
        status_code=exc.status_code,
        code=_http_code(exc.status_code),
        message=message,
        details=details,
        headers=exc.headers,
        request_id=_request_id(request),
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """把 FastAPI 的 RequestValidationError 转成不泄露输入值的 details。"""
    if not isinstance(exc, RequestValidationError):
        return await unhandled_exception_handler(request, exc)
    details = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", "无效值"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return _response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=ErrorCode.VALIDATION_ERROR,
        message="请求参数校验失败",
        details=details,
        request_id=_request_id(request),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """记录完整堆栈，但只向客户端返回通用错误文案。

    该处理器由最外层的 ServerErrorMiddleware 调用；此时请求中间件已经退出，
    contextvar 中的 request_id 会被清空，因此优先从 ``request.state`` 读取，
    并在记录日志期间重新绑定。
    """
    request_id = _request_id(request)
    token = set_request_id(request_id)
    try:
        logger.error(
            "未处理的异常",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"method": request.method, "path": request.url.path},
        )
        return _response(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="服务器内部错误，请查看日志",
            request_id=request_id,
        )
    finally:
        reset_request_id(token)


def install_error_handlers(app: FastAPI) -> None:
    """注册统一错误处理器；应在创建 FastAPI 应用后立即调用。"""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = ["install_error_handlers"]
