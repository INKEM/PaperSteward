from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from papersteward.core.entities import Paper, PaperTag
from papersteward.core.enums import AssignedBy, PaperStatus, TagRole
from papersteward.storage import (
    PaperRepository,
    PaperTagRepository,
    create_session_factory,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[2]
DOMAIN_TABLES = {
    "archive_snapshot",
    "bucket",
    "config",
    "event",
    "paper",
    "paper_bucket",
    "paper_tag",
    "rating",
    "research_profile",
    "run",
    "score_snapshot",
    "summary",
    "tag",
    "taxonomy",
}


def _alembic_config(connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.connect() as connection:
        command.upgrade(_alembic_config(connection), revision)


def _downgrade(engine: Engine, revision: str = "base") -> None:
    with engine.connect() as connection:
        command.downgrade(_alembic_config(connection), revision)


def test_upgrade_creates_formal_domain_tables(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert tables >= DOMAIN_TABLES
        assert "papers" not in tables
        assert "runs" not in tables

        assert {column["name"] for column in inspector.get_columns("paper")} == {
            "id",
            "title",
            "status",
            "authors_json",
            "venue",
            "published_at",
            "first_seen_at",
            "abstract",
            "doi",
            "arxiv_id",
            "url",
            "pdf_url",
            "oa_status",
            "canonical_id",
        }
        tag_indexes = {index["name"] for index in inspector.get_indexes("paper_tag")}
        assert "uq_paper_tag_primary" in tag_indexes
        filing_indexes = {index["name"] for index in inspector.get_indexes("paper_bucket")}
        assert "uq_paper_bucket_filing" in filing_indexes
        assert inspector.get_pk_constraint("score_snapshot")["constrained_columns"] == [
            "paper_id",
            "score_date",
        ]
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0004"


def test_downgrade_restores_p0_stub_tables(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    _downgrade(sqlite_engine, "0001")

    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert {"papers", "runs"} <= tables
        assert not (DOMAIN_TABLES & tables)
        assert "alembic_version" in tables


def test_upgrade_is_idempotent_and_can_repeat_after_downgrade(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    _upgrade(sqlite_engine)
    _downgrade(sqlite_engine)
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        assert "paper" in set(inspect(connection).get_table_names())


def test_migration_matches_models(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        command.check(_alembic_config(connection))


def test_p0_rows_are_migrated_to_formal_schema(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine, "0001")
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO papers (id, title, status, created_at) "
                "VALUES ('old-paper', 'Old Paper', 'new', '2026-10-21 08:00:00.000000')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runs (id, kind, status, started_at, finished_at, error) "
                "VALUES ('old-run', 'ingest', 'completed', "
                "'2026-10-21 08:00:00.000000', '2026-10-21 08:01:00.000000', 'boom')"
            )
        )

    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        paper = (
            connection.execute(
                text(
                    "SELECT id, title, status, authors_json, oa_status, first_seen_at "
                    "FROM paper WHERE id = 'old-paper'"
                )
            )
            .mappings()
            .one()
        )
        run = (
            connection.execute(
                text(
                    "SELECT id, run_type, status, finished_at, errors_json "
                    "FROM run WHERE id = 'old-run'"
                )
            )
            .mappings()
            .one()
        )

        assert paper["title"] == "Old Paper"
        assert paper["status"] == "discovered"
        assert json.loads(paper["authors_json"]) == []
        assert paper["oa_status"] == "unknown"
        assert paper["first_seen_at"] is not None
        assert run["run_type"] == "ingest"
        assert run["status"] == "succeeded"
        assert run["finished_at"] is not None
        assert json.loads(run["errors_json"]) == {"error": "boom"}


def test_repository_works_on_migrated_schema(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    factory = create_session_factory(sqlite_engine)

    with session_scope(factory) as session:
        PaperRepository(session).add(Paper(id="p001", title="RAG"))

    with session_scope(factory) as session:
        paper = PaperRepository(session).get("p001")
        assert paper is not None
        assert paper.title == "RAG"
        assert paper.status is PaperStatus.DISCOVERED


def test_migrated_schema_enforces_unique_primary_tag(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    factory = create_session_factory(sqlite_engine)

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        PaperRepository(session).add(Paper(id="p001", title="RAG"))
        tags = PaperTagRepository(session)
        tags.add(
            PaperTag(
                paper_id="p001",
                tag_code="T01",
                role=TagRole.PRIMARY,
                taxonomy_version="1.0",
                assigned_by=AssignedBy.USER,
            )
        )
        tags.add(
            PaperTag(
                paper_id="p001",
                tag_code="T02",
                role=TagRole.PRIMARY,
                taxonomy_version="1.0",
                assigned_by=AssignedBy.USER,
            )
        )
