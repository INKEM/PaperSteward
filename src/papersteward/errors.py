"""领域错误与统一 API 错误响应契约。

HTTP 响应信封（成功响应不使用该结构）::

    {
      "error": {
        "code": "not_found",
        "message": "文献不存在",
        "details": null,
        "request_id": "req-123"
      }
    }

``code`` 是稳定的机器可读标识；``message`` 面向用户；``details`` 放结构化
上下文，禁止放密钥；``request_id`` 用于关联服务端日志。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """稳定的错误码；新增时只追加，不重命名既有值。"""

    INVALID_REQUEST = "invalid_request"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CONFIGURATION_ERROR = "configuration_error"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"


class ErrorBody(BaseModel):
    """错误信封的负载。"""

    code: ErrorCode
    message: str
    details: Any = Field(default=None, description="结构化上下文；不得包含密钥")
    request_id: str | None = Field(default=None, description="用于关联服务端日志")


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    error: ErrorBody


class AppError(Exception):
    """所有可预期业务错误的基类；子类只覆盖类属性。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    status_code: ClassVar[int] = 500
    default_message: ClassVar[str] = "服务器内部错误"

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)

    def to_response(self, request_id: str | None = None) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                details=self.details,
                request_id=request_id,
            )
        )


class InvalidRequestError(AppError):
    """请求语义不合法（区别于 FastAPI 的参数 schema 校验）。"""

    code = ErrorCode.INVALID_REQUEST
    status_code = 400
    default_message = "请求参数不合法"


class NotFoundError(AppError):
    """资源不存在。"""

    code = ErrorCode.NOT_FOUND
    status_code = 404
    default_message = "请求的资源不存在"


class ConflictError(AppError):
    """操作与当前状态冲突，例如门槛未通过、重复提交。"""

    code = ErrorCode.CONFLICT
    status_code = 409
    default_message = "操作与当前状态冲突"


class ConfigurationError(AppError):
    """配置缺失或非法，无法继续运行。"""

    code = ErrorCode.CONFIGURATION_ERROR
    status_code = 500
    default_message = "系统配置无效"


__all__ = [
    "AppError",
    "ConfigurationError",
    "ConflictError",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "InvalidRequestError",
    "NotFoundError",
]
