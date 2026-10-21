"""请求 ID 中间件：为每个请求生成/透传关联 ID，并写入日志上下文。"""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from papersteward.observability.logging import request_id_context

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _new_request_id(incoming: str | None) -> str:
    """接受格式安全的外部 ID，否则生成新的 UUID4 hex。"""
    if incoming is not None and _REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    """绑定 request_id，并把最终 ID 回写到响应头。"""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = _new_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        with request_id_context(request_id):
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware"]
