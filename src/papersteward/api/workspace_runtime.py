"""按请求解析当前工作区，并为每个工作区持有独立运行时。

工作区身份经 cookie ``ps_workspace`` 携带；缺失或指向已删除工作区时回落默认。
默认工作区沿用启动注入的 Settings 与引擎（测试可注入）；其余工作区在首次
访问时迁移建库并惰性初始化引擎、会话工厂与任务管理器。任务互斥
（「同一时刻一个 ui.* 任务」）因此按工作区生效。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request, Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from papersteward.config import Settings
from papersteward.errors import NotFoundError
from papersteward.jobs.ui_flow import UIFlowManager
from papersteward.storage.db import create_db_engine, create_session_factory
from papersteward.storage.migrations import upgrade_head
from papersteward.storage.workspace_registry import (
    DEFAULT_WORKSPACE_ID,
    WorkspaceInfo,
    WorkspaceRegistry,
)

WORKSPACE_COOKIE = "ps_workspace"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


@dataclass
class WorkspaceRuntime:
    """一个工作区的进程内运行时；数量等于本进程访问过的工作区数。"""

    info: WorkspaceInfo
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    ui_flows: UIFlowManager
    owns_engine: bool = True

    def close(self) -> None:
        self.ui_flows.close()
        if self.owns_engine:
            self.engine.dispose()


def set_workspace_cookie(response: Response, workspace_id: str) -> None:
    """切换/创建工作区后写入身份 cookie；本地单用户，HttpOnly + Lax。"""
    response.set_cookie(
        WORKSPACE_COOKIE,
        workspace_id,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_workspace_cookie(response: Response) -> None:
    response.delete_cookie(WORKSPACE_COOKIE, path="/")


class WorkspaceRuntimeHub:
    """注册表 + 每工作区运行时的持有者；挂在 ``app.state.workspaces``。"""

    def __init__(self, settings: Settings, *, engine: Engine | None = None) -> None:
        self._settings = settings
        self._injected_engine = engine
        self._lock = threading.Lock()
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self.registry = WorkspaceRegistry(settings.data_dir)
        self.registry.reconcile()

    # —— 查询（不创建运行时） ——

    def workspaces(self) -> list[WorkspaceInfo]:
        return self.registry.list_all()

    def info(self, workspace_id: str) -> WorkspaceInfo:
        return self.registry.get(workspace_id)

    # —— 运行时 ——

    def default(self) -> WorkspaceRuntime:
        with self._lock:
            runtime = self._runtimes.get(DEFAULT_WORKSPACE_ID)
            if runtime is None:
                engine = self._injected_engine or create_db_engine(self._settings.db_url)
                factory = create_session_factory(engine)
                runtime = WorkspaceRuntime(
                    info=self.registry.get(DEFAULT_WORKSPACE_ID),
                    settings=self._settings,
                    engine=engine,
                    session_factory=factory,
                    ui_flows=UIFlowManager(factory, self._settings),
                    owns_engine=self._injected_engine is None,
                )
                self._runtimes[DEFAULT_WORKSPACE_ID] = runtime
            return runtime

    def open(self, workspace_id: str) -> WorkspaceRuntime:
        """取工作区运行时；非默认工作区首次访问时迁移建库并初始化。"""
        if workspace_id == DEFAULT_WORKSPACE_ID:
            return self.default()
        with self._lock:
            runtime = self._runtimes.get(workspace_id)
            if runtime is not None:
                return runtime
            info = self.registry.get(workspace_id)
            workspace_settings = self._settings.model_copy(
                update={"data_dir": info.directory, "database_url": None}
            )
            upgrade_head(workspace_settings)
            engine = create_db_engine(workspace_settings.db_url)
            factory = create_session_factory(engine)
            runtime = WorkspaceRuntime(
                info=info,
                settings=workspace_settings,
                engine=engine,
                session_factory=factory,
                ui_flows=UIFlowManager(factory, workspace_settings),
            )
            self._runtimes[workspace_id] = runtime
            return runtime

    def current(self, request: Request) -> WorkspaceRuntime:
        """按 cookie 解析请求所属工作区；无效时回落默认（cookie 由下次切换覆盖）。"""
        workspace_id = request.cookies.get(WORKSPACE_COOKIE, "")
        if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID:
            try:
                return self.open(workspace_id)
            except NotFoundError:
                pass
        return self.default()

    # —— 变更 ——

    def create_workspace(self, name: str, *, now: datetime) -> WorkspaceInfo:
        return self.registry.create(name, now=now)

    def remove_workspace(self, workspace_id: str, *, now: datetime) -> str:
        """删除工作区：先释放运行时（引擎句柄），再把整目录移入 backups/。"""
        self.drop(workspace_id)
        archived = self.registry.delete(workspace_id, now=now)
        return str(archived)

    def drop(self, workspace_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.pop(workspace_id, None)
        if runtime is not None:
            runtime.close()

    def close(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()


def resolve_workspace(request: Request) -> WorkspaceRuntime:
    """页面与依赖按请求解析当前工作区运行时。"""
    hub: WorkspaceRuntimeHub | None = getattr(request.app.state, "workspaces", None)
    if hub is None:
        raise RuntimeError("工作区运行时未初始化：请通过 create_app 创建应用")
    return hub.current(request)


__all__ = [
    "WORKSPACE_COOKIE",
    "WorkspaceRuntime",
    "WorkspaceRuntimeHub",
    "clear_workspace_cookie",
    "resolve_workspace",
    "set_workspace_cookie",
]
