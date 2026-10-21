from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from papersteward.storage import (
    PaperRepository,
    create_session_factory,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade(engine: Engine) -> None:
    with engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")


def _downgrade(engine: Engine) -> None:
    with engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "base")


def test_upgrade_creates_papers_and_runs(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert {"papers", "runs"} <= tables
        assert {column["name"] for column in inspector.get_columns("papers")} == {
            "id",
            "title",
            "status",
            "created_at",
        }
        assert {column["name"] for column in inspector.get_columns("runs")} == {
            "id",
            "kind",
            "status",
            "started_at",
            "finished_at",
            "error",
        }
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0001"


def test_downgrade_removes_tables_but_keeps_version_table(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    _downgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert "papers" not in tables
        assert "runs" not in tables
        assert "alembic_version" in tables


def test_upgrade_is_idempotent_and_can_repeat_after_downgrade(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    _upgrade(sqlite_engine)
    _downgrade(sqlite_engine)
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        assert "papers" in set(inspect(connection).get_table_names())


def test_migration_matches_models(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        command.check(_alembic_config(connection))


def test_repository_works_on_migrated_schema(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    factory = create_session_factory(sqlite_engine)

    with session_scope(factory) as session:
        PaperRepository(session).create("p001", "RAG")

    with session_scope(factory) as session:
        paper = PaperRepository(session).get("p001")
        assert paper is not None
        assert paper.title == "RAG"
