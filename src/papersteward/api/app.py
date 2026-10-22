"""FastAPI 应用工厂与生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from papersteward import __version__
from papersteward.api.errors import install_error_handlers
from papersteward.api.middleware import RequestIdMiddleware
from papersteward.api.onboarding import router as onboarding_router
from papersteward.api.review import router as review_router
from papersteward.api.routes import router
from papersteward.config import Settings, get_settings
from papersteward.storage import create_db_engine, create_session_factory


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    """创建 API 应用。

    - 未传 ``engine`` 时在 lifespan 中从 Settings 创建，并在关闭时 dispose；
    - 测试可注入临时 SQLite Engine，由调用方负责 dispose；
    - 不在启动时自动建表，schema 由 Alembic 管理。
    """
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_engine = engine or create_db_engine()
        app.state.engine = resolved_engine
        app.state.session_factory = create_session_factory(resolved_engine)
        try:
            yield
        finally:
            if engine is None:
                resolved_engine.dispose()

    app = FastAPI(
        title="PaperSteward",
        version=__version__,
        description="本地优先的文献自动挖掘、优先级排序与每日推送系统",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(onboarding_router)
    app.include_router(review_router)
    return app


__all__ = ["create_app"]
