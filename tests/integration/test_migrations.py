from __future__ import annotations

import json
from datetime import UTC, datetime
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
NOW_MIGRATIONS = datetime(2026, 10, 21, 8, tzinfo=UTC)
DOMAIN_TABLES = {
    "archive_snapshot",
    "bucket",
    "config",
    "enrichment",
    "event",
    "paper",
    "paper_analysis",
    "paper_bucket",
    "paper_tag",
    "rating",
    "research_profile",
    "run",
    "score_snapshot",
    "scoring_change",
    "special_nomination",
    "special_tier_snapshot",
    "static_score_snapshot",
    "review_item",
    "summary",
    "tag",
    "taxonomy",
    "view_config",
    "reading_time_revision",
    "reading_report",
    "reading_asset",
    "reading_asset_revision",
    "reading_asset_source",
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
        assert {column["name"] for column in inspector.get_columns("static_score_snapshot")} == {
            "paper_id",
            "score_date",
            "relevance",
            "authority",
            "extra_value",
            "static_score",
            "confidence",
            "config_version",
            "profile_version",
            "taxonomy_version",
            "input_hash",
            "breakdown_json",
            "created_at",
        }
        assert {column["name"] for column in inspector.get_columns("review_item")} == {
            "paper_id",
            "status",
            "reasons_json",
            "payload_json",
            "created_at",
            "updated_at",
            "resolved_at",
            "resolved_by",
            "resolution",
            "note",
        }
        assert {column["name"] for column in inspector.get_columns("special_nomination")} == {
            "id",
            "paper_id",
            "kind",
            "status",
            "confidence",
            "reason",
            "evidence_json",
            "review_layers_json",
            "classic_basis_json",
            "bridge_memo_json",
            "reasons_json",
            "provenance_model",
            "provenance_prompt_version",
            "provenance_generated_at",
            "provenance_input_hash",
            "nominated_at",
            "decided_at",
            "decided_by",
            "decision_note",
        }
        assert {column["name"] for column in inspector.get_columns("special_tier_snapshot")} == {
            "id",
            "paper_id",
            "nomination_id",
            "kind",
            "value",
            "tier",
            "dimensions_json",
            "weights_json",
            "reasons_json",
            "explanation_json",
            "evidence_json",
            "breakdown_json",
            "rubric_version",
            "assigned_by",
            "note",
            "reviewed_by",
            "created_at",
        }
        assert {column["name"] for column in inspector.get_columns("scoring_change")} == {
            "id",
            "status",
            "base_version",
            "config_version",
            "preview_day",
            "effective_on",
            "proposed_json",
            "preview_json",
            "preview_hash",
            "created_at",
            "confirmed_at",
            "confirmed_by",
            "note",
        }
        assert {column["name"] for column in inspector.get_columns("view_config")} == {
            "id",
            "name",
            "weights_json",
            "preset",
            "description",
            "created_at",
        }
        assert {column["name"] for column in inspector.get_columns("enrichment")} == {
            "paper_id",
            "teams_json",
            "venue_level",
            "citation_count",
            "citation_velocity",
            "code_url",
            "data_url",
            "oa_status",
            "experiment_quality_json",
            "confidence",
            "source",
            "created_at",
            "updated_at",
            "provenance_model",
            "provenance_prompt_version",
            "provenance_generated_at",
            "provenance_input_hash",
        }
        tag_indexes = {index["name"] for index in inspector.get_indexes("paper_tag")}
        assert "uq_paper_tag_primary" in tag_indexes
        filing_indexes = {index["name"] for index in inspector.get_indexes("paper_bucket")}
        assert "uq_paper_bucket_filing" in filing_indexes
        assert inspector.get_pk_constraint("score_snapshot")["constrained_columns"] == [
            "paper_id",
            "score_date",
        ]
        assert {column["name"] for column in inspector.get_columns("reading_time_revision")} == {
            "paper_id",
            "revision",
            "recorded_at",
            "precision",
            "declared_date",
            "declared_month",
            "basis",
            "idempotency_key",
        }
        revision_indexes = {
            index["name"] for index in inspector.get_indexes("reading_time_revision")
        }
        assert "uq_reading_time_idempotency" in revision_indexes
        assert inspector.get_pk_constraint("reading_time_revision")["constrained_columns"] == [
            "paper_id",
            "revision",
        ]
        assert {column["name"] for column in inspector.get_columns("reading_report")} == {
            "id",
            "report_kind",
            "period_label",
            "period_start",
            "period_end",
            "timezone",
            "review_mode",
            "knowledge_cutoff",
            "generated_at",
            "contract",
            "input_fingerprint",
            "content_json",
            "content_hash",
            "run_id",
        }
        report_indexes = {index["name"] for index in inspector.get_indexes("reading_report")}
        assert {
            "uq_reading_report_run",
            "uq_reading_report_fingerprint",
            "ix_reading_report_generated",
        } <= report_indexes
        for table, columns in {
            "reading_asset": {"id", "kind", "idempotency_key", "created_at"},
            "reading_asset_revision": {
                "asset_id",
                "revision",
                "recorded_at",
                "title",
                "body",
                "applicability",
                "status",
            },
            "reading_asset_source": {
                "id",
                "asset_id",
                "revision",
                "paper_id",
                "archive_hash",
                "rating_at",
            },
        }.items():
            assert {column["name"] for column in inspector.get_columns(table)} == columns
        asset_indexes = {index["name"] for index in inspector.get_indexes("reading_asset")}
        assert "uq_reading_asset_idempotency" in asset_indexes
        assert inspector.get_pk_constraint("reading_asset_revision")["constrained_columns"] == [
            "asset_id",
            "revision",
        ]
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0023"


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


def test_downgrade_0020_guarded_by_null_import_snapshots(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)
    # 直接写行绕过领域仓储（仓储自带激活门）；此处只验证迁移守卫本身。
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO paper (id, title, status, authors_json, first_seen_at, oa_status) "
                "VALUES ('p-imp', '导入论文', 'done', '[]', "
                "'2026-10-21 08:00:00.000000', 'unknown')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO archive_snapshot (paper_id, archived_at, primary_tag, "
                "filing_bucket_id, profile_version, taxonomy_version, config_version, "
                "content_hash, content_json, secondary_tags_json) VALUES ("
                "'p-imp', '2026-10-21 08:00:00.000000', NULL, NULL, "
                "'1.0', '1.0', '1.0.0', :content_hash, '{}', '[]')"
            ),
            {"content_hash": "a" * 64},
        )

    with pytest.raises(RuntimeError, match="无法回退迁移 0020"):
        _downgrade(sqlite_engine, "0019")

    with sqlite_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0020"

    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM archive_snapshot"))
        connection.execute(text("DELETE FROM paper"))

    _downgrade(sqlite_engine, "0019")
    _upgrade(sqlite_engine)


def test_migration_matches_models(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine)

    with sqlite_engine.connect() as connection:
        command.check(_alembic_config(connection))


def test_0021_roundtrip_preserves_archives_with_representative_data(
    sqlite_engine: Engine,
) -> None:
    """带代表数据的 0020 → 0021 → 0020 → 0021 往返：归档保留，日期声明随 0021 移除。"""
    _upgrade(sqlite_engine, "0020")
    factory = create_session_factory(sqlite_engine)
    with session_scope(factory) as session:
        # 无主标签的导入归档 + 有主标签的 done 归档，代表两类既有数据。
        for paper_id in ("p-import", "p-done"):
            PaperRepository(session).add(
                Paper(
                    id=paper_id,
                    title=paper_id,
                    status=PaperStatus.DONE,
                    first_seen_at=NOW_MIGRATIONS,
                )
            )
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO archive_snapshot (paper_id, archived_at, primary_tag, "
                "filing_bucket_id, profile_version, taxonomy_version, config_version, "
                "content_hash, content_json, secondary_tags_json) VALUES ("
                "'p-import', '2026-10-21 08:00:00.000000', NULL, NULL, "
                "'1.0', '1.0', '1.0.0', :import_hash, '{}', '[]'), "
                "('p-done', '2026-10-21 08:00:00.000000', 'T01', 'b1', "
                "'1.0', '1.0', '1.0.0', :done_hash, '{}', '[]')"
            ),
            {"import_hash": "b" * 64, "done_hash": "c" * 64},
        )

    _upgrade(sqlite_engine, "0021")
    with session_scope(factory) as session:
        from papersteward.core.entities import ReadingTimeRevision
        from papersteward.core.enums import ReadingTimeBasis, TimePrecision
        from papersteward.storage.insights import ReadingTimeRevisionRepository

        ReadingTimeRevisionRepository(session).add(
            ReadingTimeRevision(
                paper_id="p-import",
                revision=1,
                recorded_at=NOW_MIGRATIONS,
                precision=TimePrecision.MONTH,
                basis=ReadingTimeBasis.DECLARED,
                month="2026-09",
                idempotency_key="roundtrip-key",
            )
        )

    _downgrade(sqlite_engine, "0020")
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert "reading_time_revision" not in tables
        rows = connection.execute(
            text("SELECT paper_id FROM archive_snapshot ORDER BY paper_id")
        ).all()
        assert {row[0] for row in rows} == {"p-done", "p-import"}

    _upgrade(sqlite_engine, "0021")
    with sqlite_engine.connect() as connection:
        assert "reading_time_revision" in set(inspect(connection).get_table_names())
        # 原归档在往返后仍可读取；声明版本已被 0021 downgrade 移除（发布说明的数据损失范围）。
        count = connection.execute(text("SELECT COUNT(*) FROM archive_snapshot")).scalar()
        assert count == 2


def test_0022_roundtrip_preserves_archives_with_saved_report(sqlite_engine: Engine) -> None:
    """0021 → 0022 → 0021 → 0022 往返：归档保留，报告快照随 0022 移除。"""
    _upgrade(sqlite_engine, "0022")
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO paper (id, title, status, authors_json, first_seen_at, oa_status) "
                "VALUES ('p-r', '报告论文', 'archived', '[]', "
                "'2026-10-21 08:00:00.000000', 'unknown')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO archive_snapshot (paper_id, archived_at, primary_tag, "
                "filing_bucket_id, profile_version, taxonomy_version, config_version, "
                "content_hash, content_json, secondary_tags_json) VALUES ("
                "'p-r', '2026-10-21 08:00:00.000000', 'T01', NULL, "
                "'1.0', '1.0', '1.0.0', :content_hash, '{}', '[]')"
            ),
            {"content_hash": "d" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO reading_report (id, report_kind, period_label, period_start, "
                "period_end, timezone, review_mode, knowledge_cutoff, generated_at, contract, "
                "input_fingerprint, content_json, content_hash, run_id) VALUES ("
                "'rep-1', 'week', '2026-W43', '2026-10-19', '2026-10-25', 'Asia/Shanghai', "
                "'as_recorded', '2026-10-25 16:00:00.000000', "
                "'2026-10-26 02:00:00.000000', 'reading-insights.report/1', "
                ":fp, '{}', :hash, 'run-1')"
            ),
            {"fp": "e" * 64, "hash": "f" * 64},
        )

    _downgrade(sqlite_engine, "0021")
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert "reading_report" not in tables
        rows = connection.execute(text("SELECT paper_id FROM archive_snapshot")).all()
        assert [row[0] for row in rows] == ["p-r"]

    _upgrade(sqlite_engine, "0022")
    with sqlite_engine.connect() as connection:
        assert "reading_report" in set(inspect(connection).get_table_names())
        count = connection.execute(text("SELECT COUNT(*) FROM reading_report")).scalar()
        assert count == 0


def test_0023_roundtrip_preserves_archives_with_asset(sqlite_engine: Engine) -> None:
    """0022 → 0023 → 0022 → 0023 往返：归档与报告保留，资产随降级移除。"""
    _upgrade(sqlite_engine, "0023")
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO paper (id, title, status, authors_json, first_seen_at, oa_status) "
                "VALUES ('p-a', '资产论文', 'archived', '[]', "
                "'2026-10-21 08:00:00.000000', 'unknown')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO archive_snapshot (paper_id, archived_at, primary_tag, "
                "filing_bucket_id, profile_version, taxonomy_version, config_version, "
                "content_hash, content_json, secondary_tags_json) VALUES ("
                "'p-a', '2026-10-21 08:00:00.000000', 'T01', NULL, "
                "'1.0', '1.0', '1.0.0', :content_hash, '{}', '[]')"
            ),
            {"content_hash": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset (id, kind, idempotency_key, created_at) VALUES ("
                "'asset-1', 'problem', 'key-1', '2026-10-26 02:00:00.000000')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_revision (asset_id, revision, recorded_at, title, "
                "body, applicability, status) VALUES ("
                "'asset-1', 1, '2026-10-26 02:00:00.000000', '一个问题', "
                "'待确认的方法问题', NULL, 'open')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_source (id, asset_id, revision, paper_id, "
                "archive_hash, rating_at) VALUES ("
                "'src-1', 'asset-1', 1, 'p-a', :hash, NULL)"
            ),
            {"hash": "b" * 64},
        )

    _downgrade(sqlite_engine, "0022")
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert not {"reading_asset", "reading_asset_revision", "reading_asset_source"} & tables
        rows = connection.execute(text("SELECT paper_id FROM archive_snapshot")).all()
        assert [row[0] for row in rows] == ["p-a"]

    _upgrade(sqlite_engine, "0023")
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert {"reading_asset", "reading_asset_revision", "reading_asset_source"} <= tables
        count = connection.execute(text("SELECT COUNT(*) FROM reading_asset")).scalar()
        assert count == 0


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
