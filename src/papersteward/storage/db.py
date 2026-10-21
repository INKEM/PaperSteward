"""数据库连接、会话生命周期与 SQLite 基础设置。

P0 只建立最小可用骨架：Engine 负责连接与 SQLite 外键开关，sessionmaker 负责
事务边界，``session_scope`` 提供 CLI/任务可直接使用的提交/回滚语义。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from papersteward.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类；统一约束命名，保证迁移可复现。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间；写入数据库的时间统一使用该约定。"""
    return datetime.now(UTC)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _sqlite_engine(database_url: str, *, echo: bool, memory: bool) -> Engine:
    options: dict[str, Any] = {
        "echo": echo,
        "connect_args": {"check_same_thread": False},
    }
    if memory:
        # 内存库必须共享同一连接，否则 create_all 与后续会话看到不同的库。
        options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """创建 Engine。

    - ``database_url`` 为空时使用 ``Settings.db_url``，并确保数据目录存在；
    - SQLite 自动创建数据库文件的父目录，并启用外键约束；
    - 内存 SQLite 共享连接，便于测试与临时运行。
    """
    if database_url is None:
        settings = get_settings()
        resolved_url = settings.db_url
        if settings.database_url is None:
            settings.ensure_data_dir()
    else:
        resolved_url = database_url

    url = make_url(resolved_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if database and database != ":memory:":
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return _sqlite_engine(
            resolved_url,
            echo=echo,
            memory=database in (None, "", ":memory:"),
        )
    return create_engine(resolved_url, echo=echo)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建 Session 工厂；不自动 flush，提交后不过期，方便返回对象。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """提供一个显式事务作用域：正常提交，异常回滚，最后关闭。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine) -> bool:
    """执行 ``SELECT 1``；供后续 /health 与 doctor 判断数据库可用性。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


__all__ = [
    "Base",
    "NAMING_CONVENTION",
    "check_database",
    "create_db_engine",
    "create_session_factory",
    "session_scope",
    "utc_now",
]
