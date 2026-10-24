"""Alembic 迁移状态查询与升级，供 CLI doctor、start 与工作区建库复用。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from papersteward.config import Settings
from papersteward.storage.db import create_db_engine


def find_alembic_ini() -> Path:
    """优先当前目录，其次包所在仓库根目录，返回 alembic.ini。"""
    packaged = files("papersteward").joinpath("_migrations", "alembic.ini")
    if packaged.is_file():
        return Path(str(packaged))
    candidates = [Path.cwd() / "alembic.ini"]
    candidates.extend(parent / "alembic.ini" for parent in Path(__file__).resolve().parents)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 alembic.ini；请在项目根目录运行")


@dataclass(frozen=True)
class MigrationStatus:
    """当前数据库 revision 与脚本 head。"""

    current: str | None
    head: str | None

    @property
    def is_current(self) -> bool:
        return self.head is not None and self.current == self.head


def get_migration_status(
    engine: Engine,
    *,
    alembic_ini: Path | None = None,
) -> MigrationStatus:
    """读取数据库当前 revision 与迁移脚本 head，不执行迁移。"""
    config = Config(str(alembic_ini or find_alembic_ini()))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) == 1:
        head: str | None = heads[0]
    elif heads:
        head = ",".join(heads)
    else:
        head = None

    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return MigrationStatus(current=current, head=head)


def upgrade_head(settings: Settings) -> None:
    """把 ``settings.db_url`` 指向的库升级到迁移 head；已在 head 时为空操作。

    ``cli/start`` 启动前与工作区首次访问（新库建表）共用此实现。
    """
    config = Config(str(find_alembic_ini()))
    engine = create_db_engine(settings.db_url)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


__all__ = ["MigrationStatus", "find_alembic_ini", "get_migration_status", "upgrade_head"]
