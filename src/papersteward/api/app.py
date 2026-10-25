"""FastAPI 应用工厂与生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from papersteward import __version__
from papersteward.api.actions import router as actions_router
from papersteward.api.archive import router as archive_router
from papersteward.api.delivery import router as delivery_router
from papersteward.api.errors import install_error_handlers
from papersteward.api.governance import router as governance_router
from papersteward.api.import_pages import router as import_pages_router
from papersteward.api.imports import router as imports_router
from papersteward.api.insights import router as insights_router
from papersteward.api.insights_pages import router as insights_pages_router
from papersteward.api.jobs import router as jobs_router
from papersteward.api.middleware import RequestIdMiddleware
from papersteward.api.onboarding import router as onboarding_router
from papersteward.api.pages import router as pages_router
from papersteward.api.queue import router as queue_router
from papersteward.api.routes import router
from papersteward.api.settings import router as settings_router
from papersteward.api.settings_pages import router as settings_pages_router
from papersteward.api.setup_pages import router as setup_router
from papersteward.api.shelves import router as shelves_router
from papersteward.api.views import router as views_router
from papersteward.api.workflow_pages import router as workflow_router
from papersteward.api.workspace_pages import router as workspace_pages_router
from papersteward.config import Settings, get_settings


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    """创建 API 应用。

    - 未传 ``engine`` 时默认工作区在 lifespan 中从 Settings 创建，并在关闭时 dispose；
    - 测试可注入临时 SQLite Engine，由调用方负责 dispose；
    - 不在启动时自动建表，schema 由 Alembic 管理（工作区首次访问时迁移）；
    - 多工作区：``app.state.workspaces`` 持有注册表与每工作区运行时。
    """
    resolved_settings = settings or get_settings()
    from papersteward.api.workspace_runtime import WorkspaceRuntimeHub

    hub = WorkspaceRuntimeHub(resolved_settings, engine=engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        default_runtime = hub.default()
        app.state.engine = default_runtime.engine
        app.state.session_factory = default_runtime.session_factory
        app.state.ui_flows = default_runtime.ui_flows
        try:
            yield
        finally:
            hub.close()

    app = FastAPI(
        title="PaperSteward",
        version=__version__,
        description="本地优先的文献自动挖掘、优先级排序与每日推送系统",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.workspaces = hub
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(onboarding_router)
    app.include_router(shelves_router)
    app.include_router(views_router)
    app.include_router(governance_router)
    app.include_router(queue_router)
    app.include_router(delivery_router)
    app.include_router(actions_router)
    app.include_router(archive_router)
    app.include_router(imports_router)
    app.include_router(import_pages_router)
    app.include_router(insights_router)
    app.include_router(insights_pages_router)
    app.include_router(pages_router)
    app.include_router(settings_router)
    app.include_router(settings_pages_router)
    app.include_router(setup_router)
    app.include_router(workflow_router)
    app.include_router(workspace_pages_router)
    app.include_router(jobs_router)
    return app


__all__ = ["create_app"]
