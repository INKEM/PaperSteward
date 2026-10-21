"""ASGI 入口：``uvicorn papersteward.main:app``。"""

from papersteward.api import create_app
from papersteward.config import get_settings
from papersteward.observability.logging import configure_logging

_settings = get_settings()
configure_logging(_settings)
app = create_app(settings=_settings)

__all__ = ["app"]
