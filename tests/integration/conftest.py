"""P0-S2 集成测试 fixture：所有数据库都落在 pytest 临时目录。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from papersteward.storage import Base, create_db_engine, create_session_factory


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """空 SQLite 文件库；不自动建表，迁移测试可从零开始。"""
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def schema_engine(sqlite_engine: Engine) -> Engine:
    """已按当前模型建表的 SQLite 文件库，供仓储测试使用。"""
    Base.metadata.create_all(sqlite_engine)
    return sqlite_engine


@pytest.fixture
def session_factory(schema_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(schema_engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
