"""Alembic 迁移状态查询，供 CLI doctor 与后续检查复用。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine


def find_alembic_ini() -> Path:
    """优先当前目录，其次包所在仓库根目录，返回 alembic.ini。"""
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


__all__ = ["MigrationStatus", "find_alembic_ini", "get_migration_status"]
