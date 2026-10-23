"""用户主动重置工作区；备份与清空分别显式执行，不在启动时运行。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.llm.runtime import CONFIG_KEY
from papersteward.storage.db import Base
from papersteward.storage.repositories import ConfigRepository, RunRepository


def workspace_counts(session: Session) -> dict[str, int]:
    """只读删除范围预览，不显示内容或密钥。"""
    return {
        table.name: session.scalar(select(func.count()).select_from(table)) or 0
        for table in Base.metadata.sorted_tables
    }


def backup_workspace(engine: Engine, directory: Path, *, now: datetime) -> Path:
    database = engine.url.database
    if engine.dialect.name != "sqlite" or not database or database == ":memory:":
        raise InvalidRequestError("重置需要可备份的本地 SQLite 文件数据库")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"before-reset-{now:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.db"
    try:
        target.touch(mode=0o600, exist_ok=False)
        with (
            sqlite3.connect(
                f"file:{quote(str(Path(database).resolve()))}?mode=ro", uri=True
            ) as source,
            sqlite3.connect(target) as destination,
        ):
            source.backup(destination)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def clear_workspace(session: Session, *, keep_model: bool) -> None:
    """调用者持有提交锁及事务；清空所有业务表，保留迁移版本。"""
    if RunRepository(session).running_ui_runs():
        raise ConflictError("有任务正在运行，完成后才能重置工作区")
    model = ConfigRepository(session).latest(CONFIG_KEY) if keep_model else None
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.expunge_all()
    if model is not None:
        ConfigRepository(session).add(model)
    session.flush()
