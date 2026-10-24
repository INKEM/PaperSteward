"""共享领域错误到 HTTP 错误的边界。"""

from collections.abc import Iterator
from contextlib import contextmanager

from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError


@contextmanager
def domain_errors() -> Iterator[None]:
    try:
        yield
    except InvariantViolation as exc:
        raise ConflictError(exc.message, details={"field": exc.field}) from exc
    except DomainError as exc:
        raise InvalidRequestError(exc.message, details={"field": exc.field}) from exc
