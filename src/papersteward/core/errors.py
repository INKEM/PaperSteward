"""领域错误类型。

领域层不依赖 FastAPI 或 SQLAlchemy；异常字段名使用 ``<对象>.<字段>`` 形式，
便于命令行、API 与日志把错误定位到具体字段。
"""

from __future__ import annotations


class DomainError(ValueError):
    """领域错误的基类；``field`` 是稳定的机器可读定位符。"""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class DomainValidationError(DomainError):
    """单个实体的字段或取值不合法。"""


class InvariantViolation(DomainError):
    """跨实体、跨记录的业务不变量被破坏。"""


class InvalidTransition(InvariantViolation):
    """状态机不允许的迁移。"""


__all__ = [
    "DomainError",
    "DomainValidationError",
    "InvalidTransition",
    "InvariantViolation",
]
