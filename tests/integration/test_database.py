from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from papersteward.config import get_settings
from papersteward.storage import (
    Base,
    Paper,
    check_database,
    create_db_engine,
    create_session_factory,
    session_scope,
    utc_now,
)


def test_engine_creates_parent_directory(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "papersteward.db"
    engine = create_db_engine(f"sqlite+pysqlite:///{database}")
    try:
        assert database.parent.is_dir()
        assert not database.exists()
    finally:
        engine.dispose()


def test_engine_uses_settings_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "settings.db"
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", f"sqlite+pysqlite:///{database}")
    get_settings.cache_clear()

    engine = create_db_engine()
    try:
        assert engine.url.database == str(database)
    finally:
        engine.dispose()


def test_engine_creates_default_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.delenv("PAPERSTEWARD_DATABASE_URL", raising=False)
    monkeypatch.setenv("PAPERSTEWARD_DATA_DIR", str(data_dir))
    get_settings.cache_clear()

    engine = create_db_engine()
    try:
        assert data_dir.is_dir()
        assert engine.url.database == str(data_dir / "papersteward.db")
    finally:
        engine.dispose()


def test_sqlite_foreign_keys_are_enabled(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_in_memory_engine_shares_connection() -> None:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            session.add(Paper(id="p001", title="RAG", created_at=utc_now()))
        with session_scope(factory) as session:
            assert session.get(Paper, "p001") is not None
    finally:
        engine.dispose()


def test_check_database_returns_true(sqlite_engine: Engine) -> None:
    assert check_database(sqlite_engine) is True


def test_check_database_returns_false_for_invalid_file(tmp_path: Path) -> None:
    not_a_database = tmp_path / "not-a-database"
    not_a_database.mkdir()
    engine = create_db_engine(f"sqlite+pysqlite:///{not_a_database}")
    try:
        assert check_database(engine) is False
    finally:
        engine.dispose()


def test_session_scope_commits(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        session.add(Paper(id="p001", title="RAG", created_at=utc_now()))

    with session_scope(session_factory) as session:
        assert session.get(Paper, "p001") is not None


def test_session_scope_rolls_back_on_error(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="stop"), session_scope(session_factory) as session:
        session.add(Paper(id="p001", title="RAG", created_at=utc_now()))
        raise RuntimeError("stop")

    with session_scope(session_factory) as session:
        assert session.get(Paper, "p001") is None
