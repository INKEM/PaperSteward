from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete, func, inspect, select, text
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
    "reading_memory",
    "reading_memory_revision",
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
    "reading_report_context",
    "reading_asset",
    "reading_asset_revision",
    "reading_asset_source",
    "reading_write_receipt",
    "reading_derived_copy",
    "reading_structured_feedback",
    "reading_analysis_grant",
    "reading_analysis_revoke_event",
    "reading_analysis_manifest",
    "reading_analysis_preview",
    "reading_analysis_excerpt",
    "reading_analysis_candidate",
    "reading_asset_typed",
    "reading_analysis_decision",
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
        assert {column["name"] for column in inspector.get_columns("reading_report_context")} == {
            "report_id",
            "context_json",
            "series_key",
        }
        assert "ix_reading_report_context_series" in {
            index["name"] for index in inspector.get_indexes("reading_report_context")
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
        assert {column["name"] for column in inspector.get_columns("reading_write_receipt")} == {
            "id",
            "idempotency_key",
            "operation",
            "target_id",
            "request_hash",
            "result_json",
            "created_at",
        }
        receipt_indexes = {
            index["name"] for index in inspector.get_indexes("reading_write_receipt")
        }
        assert "uq_reading_write_receipt_key" in receipt_indexes
        assert {column["name"] for column in inspector.get_columns("reading_derived_copy")} == {
            "id",
            "source_kind",
            "source_id",
            "source_revision",
            "derived_kind",
            "derived_id",
            "field_kind",
            "created_at",
        }
        derived_indexes = {index["name"] for index in inspector.get_indexes("reading_derived_copy")}
        assert {
            "ix_reading_derived_copy_source",
            "ix_reading_derived_copy_target",
            "uq_reading_derived_copy_entry",
        } <= derived_indexes
        asset_indexes = {index["name"] for index in inspector.get_indexes("reading_asset")}
        assert "uq_reading_asset_idempotency" in asset_indexes
        assert {
            column["name"] for column in inspector.get_columns("reading_structured_feedback")
        } == {
            "paper_id",
            "revision",
            "recorded_at",
            "rating_at",
            "value_sources_json",
            "limited_reasons_json",
            "context_note",
            "applicability",
            "takeaway",
            "question",
            "usage_result",
            "vocabulary_version",
            "idempotency_key",
        }
        structured_indexes = {
            index["name"] for index in inspector.get_indexes("reading_structured_feedback")
        }
        assert "uq_reading_structured_feedback_key" in structured_indexes
        assert inspector.get_pk_constraint("reading_structured_feedback")[
            "constrained_columns"
        ] == ["paper_id", "revision"]
        assert inspector.get_pk_constraint("reading_asset_revision")["constrained_columns"] == [
            "asset_id",
            "revision",
        ]
        assert {column["name"] for column in inspector.get_columns("reading_analysis_grant")} == {
            "id",
            "purpose",
            "scope_mode",
            "endpoint_identity",
            "model_config_version",
            "model_name",
            "material_classes_json",
            "manifest_hash",
            "consent_version",
            "created_at",
            "revoked_at",
        }
        assert {column["name"] for column in inspector.get_columns("reading_analysis_excerpt")} == {
            "id",
            "paper_id",
            "content",
            "source_note",
            "content_hash",
            "recorded_at",
        }
        excerpt_indexes = {
            index["name"] for index in inspector.get_indexes("reading_analysis_excerpt")
        }
        assert "uq_reading_analysis_excerpt" in excerpt_indexes
        assert {
            column["name"] for column in inspector.get_columns("reading_analysis_candidate")
        } == {
            "id",
            "run_id",
            "manifest_id",
            "manifest_hash",
            "grant_id",
            "model_config_version",
            "paper_id",
            "kind",
            "nature",
            "statement",
            "conditions",
            "limitations",
            "quote",
            "quote_source_id",
            "source_refs_json",
            "verification",
            "stale",
            "created_at",
        }
        candidate_indexes = {
            index["name"] for index in inspector.get_indexes("reading_analysis_candidate")
        }
        assert {
            "ix_reading_analysis_candidate_run",
            "ix_reading_analysis_candidate_manifest",
            "ix_reading_analysis_candidate_paper",
        } <= candidate_indexes
        assert {column["name"] for column in inspector.get_columns("reading_asset_typed")} == {
            "asset_id",
            "revision",
            "content_json",
        }
        assert {
            column["name"] for column in inspector.get_columns("reading_analysis_decision")
        } == {
            "id",
            "candidate_id",
            "decision",
            "asset_id",
            "asset_revision",
            "paper_id",
            "kind",
            "statement_hash",
            "note",
            "decided_at",
        }
        decision_indexes = {
            index["name"] for index in inspector.get_indexes("reading_analysis_decision")
        }
        assert {
            "uq_reading_analysis_decision_candidate",
            "ix_reading_analysis_decision_memory",
            "ix_reading_analysis_decision_asset",
        } <= decision_indexes
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0032"


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


@pytest.mark.parametrize("target", ["0021", "0023"])
def test_0021_roundtrip_preserves_archives_with_representative_data(
    sqlite_engine: Engine,
    target: str,
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

    _upgrade(sqlite_engine, target)
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

    _upgrade(sqlite_engine, target)
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


def test_0024_context_backfill_keeps_frozen_reports_and_unknowns(sqlite_engine: Engine) -> None:
    """旧请求只恢复有证据的字段；无运行记录的主题不默认为全部，往返不改正文。

    冻结正文按 0024 之前代码的产物形态手工构造（对齐 0026 测试做法），
    避免在旧 schema 上运行新版投影代码。
    """
    from papersteward.core.entities import Run
    from papersteward.reading.insights.reports import read_report, report_input_status
    from papersteward.storage.delivery import content_hash
    from papersteward.storage.models import ReadingReportContextRow, ReadingReportRow, RunRow
    from papersteward.storage.repositories import RunRepository

    _upgrade(sqlite_engine, "0023")
    from conftest import activate_workspace

    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)
    from test_reading_insight_projection import seed_archive, seed_paper, system_content

    from papersteward.storage.models import (
        ReadingAssetRevisionRow,
        ReadingAssetRow,
        ReadingAssetSourceRow,
        ReadingTimeRevisionRow,
    )

    at = NOW_MIGRATIONS - timedelta(hours=2)
    from papersteward.storage.delivery import content_hash as _content_hash

    legacy_content = system_content(title="旧冻结论文", completed_at=at)
    with session_scope(factory) as session:
        seed_paper(session, "legacy-paper", first_seen_at=at)
        seed_archive(
            session,
            "legacy-paper",
            archived_at=at,
            content=legacy_content,
        )
        # 直接写旧行形态（无回执），模拟旧版本代码的产物。
        session.add(
            ReadingAssetRow(
                id="legacy-asset-row",
                kind="problem",
                idempotency_key="legacy-asset",
                created_at=at,
            )
        )
        session.flush()
        session.add(
            ReadingAssetRevisionRow(
                asset_id="legacy-asset-row",
                revision=1,
                recorded_at=at,
                title="旧私人标题",
                body="旧私人正文",
                applicability=None,
                status="open",
            )
        )
        session.add(
            ReadingAssetSourceRow(
                id="legacy-src-1",
                asset_id="legacy-asset-row",
                revision=1,
                paper_id="legacy-paper",
                archive_hash=_content_hash(legacy_content),
                rating_at=None,
            )
        )
        session.add(
            ReadingTimeRevisionRow(
                paper_id="legacy-paper",
                revision=1,
                recorded_at=at,
                precision="day",
                declared_date=at.date(),
                declared_month=None,
                basis="declared",
                idempotency_key="legacy-time",
            )
        )

    def related_state():
        with sqlite_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM {name}")).all()
                for name in (
                    "reading_asset",
                    "reading_asset_revision",
                    "reading_asset_source",
                    "reading_time_revision",
                )
            }

    before = related_state()
    assert all(before.values())
    frozen = {
        "contract": "reading-insights.report/1",
        "metrics": {
            "period": {
                "kind": "week",
                "timezone": "Asia/Shanghai",
                "label": "2026-W43",
                "start_date": "2026-10-19",
                "end_date_inclusive": "2026-10-25",
                "start_utc": "2026-10-18T16:00:00Z",
                "end_utc_exclusive": "2026-10-25T16:00:00Z",
            },
            "mode": "as_recorded",
            "knowledge_cutoff": NOW_MIGRATIONS.isoformat(),
            "collected_at": NOW_MIGRATIONS.isoformat(),
            "readings_total": 1,
            "readings_system": 1,
            "readings_imported": 0,
            "readings_backfilled": 0,
            "system_completions": 1,
            "backfills_total": 0,
            "unassigned_total": 0,
            "partial_month_total": 0,
            "coverage": {"scored": 1, "noted": 0, "total": 1},
            "backfill_coverage": {"scored": 0, "noted": 0, "total": 0},
            "score_distribution": {
                "score_1": 0,
                "score_2": 0,
                "score_3": 0,
                "score_4": 1,
                "score_5": 0,
                "unrated": 0,
            },
            "feedback_changes": {"score_events": 0, "note_events": 0, "papers_affected": 0},
            "all_history_total": 1,
        },
        "readings": [
            {
                "paper_id": "legacy-paper",
                "archived_at": at.isoformat(),
                "source": "system",
                "content_available": True,
                "reading": {
                    "precision": "day",
                    "basis": "declared",
                    "day": at.date().isoformat(),
                    "month": None,
                    "recorded_at_utc": at.isoformat(),
                },
                "membership": "in_period",
                "recorded_in_period": True,
                "title": "旧冻结论文",
                "feedback": [],
            }
        ],
        "backfills": [],
        "unassigned": [],
        "changes": [],
        "assets": [
            {
                "asset_id": "legacy-asset-row",
                "title": "旧私人标题",
                "kind": "problem",
                "status": "open",
                "body": "旧私人正文",
            }
        ],
    }
    assert frozen["assets"][0]["title"] == "旧私人标题"
    frozen_hash = content_hash(frozen)
    requests = {
        "explicit": {
            "review": {
                "period": "week",
                "anchor_date": "2026-10-22",
                "mode": "as_recorded",
                "tag": "tv-test::T01",
                "asset_bodies": False,
            }
        },
        "omitted": {
            "review": {"period": "week", "anchor_date": "2026-10-22", "mode": "as_recorded"}
        },
        "all": {
            "review": {
                "period": "week",
                "anchor_date": "2026-10-22",
                "mode": "as_recorded",
                "tag": None,
            }
        },
    }
    with session_scope(factory) as session:
        for key in ("explicit", "omitted", "all", "missing-run"):
            if key in requests:
                RunRepository(session).add(
                    Run(
                        id=key,
                        run_type="ui.review",
                        started_at=NOW_MIGRATIONS,
                        stats_json={"request": requests[key]},
                    )
                )
            session.add(
                ReadingReportRow(
                    id=key,
                    report_kind="week",
                    period_label="2026-W43",
                    period_start=date(2026, 10, 19),
                    period_end=date(2026, 10, 25),
                    timezone="Asia/Shanghai",
                    review_mode="as_recorded",
                    knowledge_cutoff=NOW_MIGRATIONS,
                    generated_at=NOW_MIGRATIONS,
                    contract=frozen["contract"],
                    input_fingerprint=content_hash({"fixture": key}),
                    content_json=frozen,
                    content_hash=frozen_hash,
                    run_id=key,
                )
            )
    _upgrade(sqlite_engine, "0024")
    assert related_state() == before
    with session_scope(factory) as session:
        explicit = read_report(session, "explicit")
        assert explicit.context.scope.topic.code == "T01"
        assert explicit.context.scope.topic.taxonomy_version == "tv-test"
        assert explicit.context.scope.algorithm_version is None
        assert explicit.context.status == "legacy_unknown"
        assert explicit.context.scope.topic_known is True
        assert read_report(session, "all").context.scope.topic_known is True
        assert read_report(session, "all").context.scope.topic is None
        for key in ("omitted", "missing-run"):
            detail = read_report(session, key)
            assert detail.context.scope.topic_known is False
            assert report_input_status(session, detail, now=NOW_MIGRATIONS)["changed"] is None
        row = session.get(ReadingReportRow, "explicit")
        assert row.content_json == frozen and row.content_hash == frozen_hash
        session.execute(delete(RunRow))
    with session_scope(factory) as session:
        assert read_report(session, "explicit").context.scope.topic.code == "T01"
        assert session.get(ReadingReportContextRow, "explicit").series_key is None
    _downgrade(sqlite_engine, "0023")
    assert related_state() == before
    with session_scope(factory) as session:
        row = session.get(ReadingReportRow, "explicit")
        assert row.content_json == frozen and row.content_hash == frozen_hash
    _upgrade(sqlite_engine, "0024")
    assert related_state() == before
    with session_scope(factory) as session:
        assert read_report(session, "explicit").context.scope.topic_known is False
        assert session.get(ReadingReportRow, "explicit").content_json == frozen


def test_0025_receipt_backfill_recovers_request_hashes(sqlite_engine: Engine) -> None:
    """既有键回填的请求哈希必须与服务写入时的哈希一致；业务行在往返中原样保留。"""
    from test_reading_insight_projection import seed_archive, seed_paper, system_content

    from conftest import activate_workspace
    from papersteward.core.entities import Rating
    from papersteward.reading.insights.assets import create_asset
    from papersteward.reading.insights.contracts import AssetCreateInput, ReadTimeDeclaration
    from papersteward.reading.insights.idempotency import (
        ASSET_CREATE,
        READING_TIME_RECORD,
        asset_content_payload,
        reading_time_payload,
        request_hash,
    )
    from papersteward.reading.insights.timeline import record_reading_time
    from papersteward.storage.mappers import as_utc
    from papersteward.storage.repositories import RatingRepository

    _upgrade(sqlite_engine)
    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)

    at = NOW_MIGRATIONS - timedelta(hours=2)
    with session_scope(factory) as session:
        seed_paper(session, "receipt-paper", first_seen_at=at)
        seed_archive(
            session,
            "receipt-paper",
            archived_at=at,
            content=system_content(title="回执种子论文", completed_at=at),
        )
        RatingRepository(session).add(
            Rating("receipt-paper", 4, "有具体反馈", (), None, as_utc(at))
        )
        create_asset(
            session,
            AssetCreateInput(
                kind="harvest",
                title="回执资产",
                body="正文内容",
                sources=[{"paper_id": "receipt-paper", "rating_at": as_utc(at)}],
                idempotency_key="receipt-asset-key",
            ),
            now=at,
        )
        record_reading_time(
            session,
            "receipt-paper",
            ReadTimeDeclaration(precision="day", day=at.date()),
            now=at,
            expected_revision=0,
            idempotency_key="receipt-time-key",
        )

    def receipts() -> dict[str, dict]:
        with sqlite_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT idempotency_key, operation, target_id, request_hash, result_json"
                    " FROM reading_write_receipt"
                )
            ).all()
        return {
            row.idempotency_key: {
                "operation": row.operation,
                "target_id": row.target_id,
                "request_hash": row.request_hash,
                "result_json": row.result_json,
            }
            for row in rows
        }

    def business_state() -> dict:
        with sqlite_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM {name}")).all()
                for name in (
                    "reading_asset",
                    "reading_asset_revision",
                    "reading_asset_source",
                    "reading_time_revision",
                )
            }

    written = receipts()
    before = business_state()
    _downgrade(sqlite_engine, "0024")
    assert business_state() == before
    _upgrade(sqlite_engine, "0025")
    assert business_state() == before
    recovered = receipts()
    # 回填恢复的哈希与降级前服务写入的哈希完全一致：同一规范化口径。
    assert recovered == {
        "receipt-time-key": {
            "operation": "reading_time.record",
            "target_id": "receipt-paper",
            "request_hash": written["receipt-time-key"]["request_hash"],
            "result_json": None,
        },
        "receipt-asset-key": {
            "operation": "asset.create",
            "target_id": "",
            "request_hash": written["receipt-asset-key"]["request_hash"],
            "result_json": None,
        },
    }
    # 哈希可由共享规范化函数从存储行重现。
    assert recovered["receipt-time-key"]["request_hash"] == request_hash(
        READING_TIME_RECORD,
        "receipt-paper",
        reading_time_payload(
            paper_id="receipt-paper",
            precision="day",
            day=at.date().isoformat(),
            month=None,
        ),
    )
    assert recovered["receipt-asset-key"]["request_hash"] == request_hash(
        ASSET_CREATE,
        "",
        asset_content_payload(
            "harvest",
            title="回执资产",
            body="正文内容",
            applicability=None,
            sources=[("receipt-paper", as_utc(at).isoformat())],
        ),
    )


def test_0026_derived_copy_backfill_registers_private_fields(sqlite_engine: Engine) -> None:
    """既有报告的私人字段按冻结正文登记；旧报告缺版本记 None，往返不改业务行。"""
    from papersteward.storage.delivery import content_hash
    from papersteward.storage.models import ReadingDerivedCopyRow, ReadingReportRow

    _upgrade(sqlite_engine, "0025")
    factory = create_session_factory(sqlite_engine)
    at = NOW_MIGRATIONS - timedelta(hours=1)
    frozen = {
        "contract": "reading-insights.report/1",
        "metrics": {},
        "readings": [],
        "backfills": [],
        "unassigned": [],
        "changes": [],
        "assets": [
            {
                "asset_id": "copy-asset",
                "title": "私人标题",
                "kind": "problem",
                "status": "open",
                "body": "私人正文",
                "revision": 3,
                "source_paper_ids": ["copy-paper"],
                "cross_topic": False,
            },
            {
                # 旧报告形态：无 revision、无正文副本。
                "asset_id": "copy-asset",
                "title": "私人标题",
                "kind": "problem",
                "status": "open",
                "body": None,
                "source_paper_ids": ["copy-paper"],
                "cross_topic": False,
            },
        ],
    }
    with session_scope(factory) as session:
        for key in ("copy-report-a", "copy-report-b"):
            session.add(
                ReadingReportRow(
                    id=key,
                    report_kind="week",
                    period_label="测试周",
                    period_start=None,
                    period_end=None,
                    timezone="Asia/Shanghai",
                    review_mode="as_recorded",
                    knowledge_cutoff=at,
                    generated_at=at,
                    contract="reading-insights.report/1",
                    input_fingerprint=content_hash({"fixture": key}),
                    content_json=frozen,
                    content_hash=content_hash(frozen),
                    run_id=key,
                )
            )

    def derived_rows() -> list[tuple]:
        with sqlite_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT source_kind, source_id, source_revision, derived_kind,"
                    " derived_id, field_kind FROM reading_derived_copy"
                    " ORDER BY derived_id, source_revision, field_kind"
                )
            ).all()

    def business_state() -> dict:
        with sqlite_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM {name}")).all()
                for name in ("reading_report", "reading_asset", "reading_write_receipt")
            }

    before = business_state()
    _upgrade(sqlite_engine, "0026")
    expected = [
        # SQLite ASC 排序中 NULL 在最前。
        ("asset", "copy-asset", None, "report", "copy-report-a", "title"),
        ("asset", "copy-asset", 3, "report", "copy-report-a", "body"),
        ("asset", "copy-asset", 3, "report", "copy-report-a", "title"),
        ("asset", "copy-asset", None, "report", "copy-report-b", "title"),
        ("asset", "copy-asset", 3, "report", "copy-report-b", "body"),
        ("asset", "copy-asset", 3, "report", "copy-report-b", "title"),
    ]
    assert derived_rows() == expected
    assert business_state() == before

    _downgrade(sqlite_engine, "0025")
    assert business_state() == before
    _upgrade(sqlite_engine, "0026")
    assert business_state() == before
    assert derived_rows() == expected
    with session_scope(factory) as session:
        count = session.scalar(select(func.count()).select_from(ReadingDerivedCopyRow))
    assert count == len(expected)


def test_0027_receipt_rebuild_preserves_rows_and_extends_operations(
    sqlite_engine: Engine,
) -> None:
    """回执表重建逐行保留既有回执；新 CHECK 接受结构化操作码，降级移除。"""
    from test_reading_insight_projection import seed_archive, seed_paper, system_content

    from conftest import activate_workspace
    from papersteward.reading.insights.contracts import ReadTimeDeclaration
    from papersteward.reading.insights.timeline import record_reading_time

    _upgrade(sqlite_engine, "0026")
    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)

    at = NOW_MIGRATIONS - timedelta(hours=2)
    with session_scope(factory) as session:
        seed_paper(session, "sf-paper", first_seen_at=at)
        seed_archive(
            session,
            "sf-paper",
            archived_at=at,
            content=system_content(title="结构化种子论文", completed_at=at),
        )
        record_reading_time(
            session,
            "sf-paper",
            ReadTimeDeclaration(precision="day", day=at.date()),
            now=at,
            expected_revision=0,
            idempotency_key="sf-time-key",
        )

    def receipts() -> list[tuple]:
        with sqlite_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT idempotency_key, operation, target_id, request_hash,"
                    " result_json, created_at FROM reading_write_receipt"
                    " ORDER BY idempotency_key"
                )
            ).all()
        return [tuple(row) for row in rows]

    before = receipts()
    assert before, "种子写入应产生既有操作码回执行"

    # 0026 的 CHECK 拒绝新操作码（验证扩展确实由本迁移引入）。
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_write_receipt (id, idempotency_key, operation,"
                " target_id, request_hash, result_json, created_at) VALUES ("
                "'rej', 'newop-key', 'structured_feedback.revise', 'sf-paper',"
                " NULL, NULL, '2026-10-21 08:00:00.000000')"
            )
        )

    _upgrade(sqlite_engine, "0027")
    assert receipts() == before, "回执表重建必须逐行保留既有数据"

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_write_receipt (id, idempotency_key, operation,"
                " target_id, request_hash, result_json, created_at) VALUES ("
                "'acc', 'sf-key-1', 'structured_feedback.revise', 'sf-paper',"
                " 'aabbccdd', NULL, '2026-10-21 08:00:00.000000')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reading_structured_feedback (paper_id, revision, recorded_at,"
                " rating_at, value_sources_json, limited_reasons_json, context_note,"
                " applicability, takeaway, question, usage_result, vocabulary_version,"
                " idempotency_key) VALUES ("
                "'sf-paper', 1, '2026-10-21 08:00:00.000000', NULL, '[\"aids_understanding\"]',"
                " '[]', NULL, NULL, '学到了X', NULL, NULL, 'reading-feedback.vocab/1', 'sf-key-1')"
            )
        )

    _downgrade(sqlite_engine, "0026")
    assert receipts() == before, "降级移除新操作码回执，旧回执原样保留"
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    assert "reading_structured_feedback" not in tables

    _upgrade(sqlite_engine, "0027")
    assert receipts() == before, "再升级不恢复已删除的数据"


def test_0028_analysis_tables_roundtrip_with_data(sqlite_engine: Engine) -> None:
    """0028 全为新表：head 种子后降级 0027 整体删除，再升级为空表。"""
    from test_reading_insight_projection import seed_paper

    from conftest import activate_workspace

    _upgrade(sqlite_engine)
    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)
    at = NOW_MIGRATIONS - timedelta(hours=1)

    with session_scope(factory) as session:
        seed_paper(session, "ana-paper", first_seen_at=at)
        connection = session.connection()
        connection.execute(
            text(
                "INSERT INTO reading_analysis_grant (id, purpose, scope_mode,"
                " endpoint_identity, model_config_version, model_name,"
                " material_classes_json, manifest_hash, consent_version, created_at,"
                " revoked_at) VALUES ('g1', 'reading_reflection', 'workspace',"
                " 'http://127.0.0.1:9/v1', 'v-test', 'test-model',"
                " '[\"user_notes\"]', NULL, 'reading-analysis.consent/1',"
                " :at, NULL)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_analysis_revoke_event (id, grant_id, purpose,"
                " reason, created_at) VALUES ('r1', 'g1', 'reading_reflection',"
                " 'user', :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_analysis_manifest (id, purpose, manifest_hash,"
                " content_json, created_at) VALUES ('m1', 'reading_reflection',"
                " 'a'*64, '{}', :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_analysis_preview (id, token, manifest_id,"
                " manifest_hash, purpose, model_config_version, expires_at,"
                " consumed_at, created_at) VALUES ('p1', 'tok', 'm1', 'a'*64,"
                " 'reading_reflection', 'v-test', :expires, NULL, :at)"
            ),
            {"at": at.isoformat(), "expires": (at + timedelta(minutes=15)).isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_analysis_excerpt (id, paper_id, content,"
                " source_note, content_hash, recorded_at) VALUES ('e1', 'ana-paper',"
                " '摘录内容', '第 2 页', 'b'*64, :at)"
            ),
            {"at": at.isoformat()},
        )

    def analysis_state() -> dict:
        with sqlite_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM {name}")).all()
                for name in (
                    "reading_analysis_grant",
                    "reading_analysis_revoke_event",
                    "reading_analysis_manifest",
                    "reading_analysis_preview",
                    "reading_analysis_excerpt",
                )
            }

    before = analysis_state()
    assert all(len(rows) == 1 for rows in before.values())

    _upgrade(sqlite_engine)
    assert analysis_state() == before

    _downgrade(sqlite_engine, "0027")
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    assert not set(before) & tables

    _upgrade(sqlite_engine, "0028")
    assert all(not rows for rows in analysis_state().values())


def test_0029_derived_copy_rebuild_and_candidate_roundtrip(sqlite_engine: Engine) -> None:
    """登记行经重建逐行不变；候选随升级可用；降级遇候选登记明确拒绝。"""
    from test_reading_insight_projection import seed_paper

    from conftest import activate_workspace

    _upgrade(sqlite_engine, "0028")
    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)
    at = NOW_MIGRATIONS - timedelta(hours=1)
    with session_scope(factory) as session:
        seed_paper(session, "cand-paper", first_seen_at=at)
        from papersteward.storage.delivery import content_hash
        from papersteward.storage.models import ReadingReportRow

        frozen = {"contract": "reading-insights.report/1", "metrics": {}}
        session.add(
            ReadingReportRow(
                id="rep1",
                report_kind="week",
                period_label="2026-W43",
                period_start=None,
                period_end=None,
                timezone="Asia/Shanghai",
                review_mode="as_recorded",
                knowledge_cutoff=at,
                generated_at=at,
                contract="reading-insights.report/1",
                input_fingerprint=content_hash({"fixture": "rep1"}),
                content_json=frozen,
                content_hash=content_hash(frozen),
                run_id="rep1",
            )
        )
        session.flush()
        connection = session.connection()
        # 既有报告登记行（旧 source_kind/field_kind 组合）
        connection.execute(
            text(
                "INSERT INTO reading_derived_copy (id, source_kind, source_id,"
                " source_revision, derived_kind, derived_id, field_kind, created_at)"
                " VALUES ('d1', 'asset', 'a1', 2, 'report', 'rep1', 'title', :at)"
            ),
            {"at": at.isoformat()},
        )

    def derived_rows() -> list[tuple]:
        with sqlite_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, source_kind, source_id, source_revision, derived_kind,"
                    " derived_id, field_kind FROM reading_derived_copy"
                    " ORDER BY id"
                )
            ).fetchall()
        return [tuple(row) for row in rows]

    before = derived_rows()
    assert before == [("d1", "asset", "a1", 2, "report", "rep1", "title")]

    _upgrade(sqlite_engine, "0029")
    assert derived_rows() == before, "重建必须逐行保留既有登记"

    with session_scope(factory) as session:
        connection = session.connection()
        connection.execute(
            text(
                "INSERT INTO reading_analysis_candidate (id, run_id, manifest_id,"
                " manifest_hash, grant_id, model_config_version, paper_id, kind,"
                " nature, statement, conditions, limitations, quote, quote_source_id,"
                " source_refs_json, verification, stale, created_at) VALUES ("
                "'c1', 'run-1', 'm1', 'a'*64, 'g1', 'v1', 'cand-paper', 'harvest',"
                " 'model_inference', '候选陈述', NULL, NULL, NULL, NULL, '[]',"
                " 'pending_user', 0, :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_derived_copy (id, source_kind, source_id,"
                " source_revision, derived_kind, derived_id, field_kind, created_at)"
                " VALUES ('d2', 'paper', 'cand-paper', NULL, 'candidate', 'c1',"
                " 'statement', :at)"
            ),
            {"at": at.isoformat()},
        )

    # 降级遇候选登记明确拒绝；清掉候选与登记后可降级且旧登记保留。
    with pytest.raises(RuntimeError, match="0029"):
        _downgrade(sqlite_engine, "0028")
    with session_scope(factory) as session:
        connection = session.connection()
        connection.execute(text("DELETE FROM reading_derived_copy WHERE id = 'd2'"))
        connection.execute(text("DELETE FROM reading_analysis_candidate WHERE id = 'c1'"))
    _downgrade(sqlite_engine, "0028")
    assert derived_rows() == before
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    assert "reading_analysis_candidate" not in tables

    _upgrade(sqlite_engine, "0029")
    assert derived_rows() == before


def test_0030_asset_rebuild_and_decisions_roundtrip(sqlite_engine: Engine) -> None:
    """资产三表经重建逐行不变；typed/decision 随升级可用；降级有守卫。"""
    from test_reading_insight_projection import seed_paper

    from conftest import activate_workspace

    _upgrade(sqlite_engine, "0029")
    activate_workspace(sqlite_engine)
    factory = create_session_factory(sqlite_engine)
    at = NOW_MIGRATIONS - timedelta(hours=1)
    with session_scope(factory) as session:
        seed_paper(session, "d-paper", first_seen_at=at)
        from test_reading_insight_projection import seed_archive, system_content

        seed_archive(
            session,
            "d-paper",
            archived_at=at,
            content=system_content(title="决定种子", completed_at=at),
            rating_col=4,
            ratings=[(at, 4, None)],
        )
        # 裸 SQL 种旧行形态（0029 时不跑含 typed 查询的新版 ORM 代码）。
        connection = session.connection()
        connection.execute(
            text(
                "INSERT INTO reading_asset (id, kind, idempotency_key, created_at)"
                " VALUES ('old-asset', 'problem', 'd-asset-key', :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_revision (asset_id, revision, recorded_at,"
                " title, body, applicability, status) VALUES"
                " ('old-asset', 1, :at, '旧资产标题', '旧资产正文', NULL, 'open')"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_source (id, asset_id, revision, paper_id,"
                " archive_hash, rating_at) VALUES"
                " ('old-src', 'old-asset', 1, 'd-paper', 'c'*64, :rating)"
            ),
            {"rating": at.isoformat()},
        )

    def asset_state() -> dict:
        with sqlite_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM {name} ORDER BY 1")).all()
                for name in (
                    "reading_asset",
                    "reading_asset_revision",
                    "reading_asset_source",
                )
            }

    before = asset_state()
    assert before["reading_asset"], "种子资产行必须存在"

    _upgrade(sqlite_engine, "0030")
    assert asset_state() == before, "三表重建必须逐行保留"

    with session_scope(factory) as session:
        connection = session.connection()
        connection.execute(
            text(
                "INSERT INTO reading_asset (id, kind, idempotency_key, created_at)"
                " VALUES ('m1', 'method', 'm-key', :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_revision (asset_id, revision, recorded_at,"
                " title, body, applicability, status) VALUES ('m1', 1, :at,"
                " '方法卡', '关键做法正文', NULL, 'active')"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_asset_typed (asset_id, revision, content_json)"
                ' VALUES (\'m1\', 1, \'{"problem": "P", "approach": "A"}\')'
            )
        )
        connection.execute(
            text(
                "INSERT INTO reading_analysis_decision (id, candidate_id, decision,"
                " asset_id, asset_revision, paper_id, kind, statement_hash, note,"
                " decided_at) VALUES ('dec1', 'c1', 'confirmed', 'm1', 1,"
                " 'd-paper', 'method', 'a'*64, NULL, :at)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "UPDATE reading_analysis_candidate SET verification = 'confirmed'"
                " WHERE id = 'x'"  # 无行，占位保证 UPDATE 语法可见性
            )
        )

    # 降级守卫：新 kind 资产存在即拒绝
    with pytest.raises(RuntimeError, match="0030"):
        _downgrade(sqlite_engine, "0029")

    # 清掉新类型与决定 → 可降级且旧数据保留
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM reading_analysis_decision"))
        connection.execute(text("DELETE FROM reading_asset_typed"))
        connection.execute(text("DELETE FROM reading_asset WHERE kind = 'method'"))
    _downgrade(sqlite_engine, "0029")
    assert asset_state() == before
    with sqlite_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    assert "reading_asset_typed" not in tables
    assert "reading_analysis_decision" not in tables

    _upgrade(sqlite_engine, "0030")
    assert asset_state() == before


def test_0031_synthesis_grant_and_derived_copy_roundtrip(sqlite_engine: Engine) -> None:
    """授权用途与私人综合登记扩展：旧行保留，新值可写，降级遇新值拒绝。"""
    _upgrade(sqlite_engine, "0030")
    at = NOW_MIGRATIONS - timedelta(hours=1)
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_analysis_grant (id, purpose, scope_mode,"
                " endpoint_identity, model_config_version, model_name,"
                " material_classes_json, manifest_hash, consent_version, created_at,"
                " revoked_at) VALUES ('g-old', 'reading_reflection', 'workspace',"
                " 'http://127.0.0.1:9/v1', 'v-test', 'test-model',"
                " '[\"user_notes\"]', NULL, 'reading-analysis.consent/1', :at, NULL)"
            ),
            {"at": at.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO reading_derived_copy (id, source_kind, source_id,"
                " source_revision, derived_kind, derived_id, field_kind, created_at)"
                " VALUES ('d-old', 'asset', 'asset-1', 1, 'report', 'report-1',"
                " 'title', :at)"
            ),
            {"at": at.isoformat()},
        )
    # 0030 的 CHECK 拒绝新用途与新登记值（验证扩展确由本迁移引入）。
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_analysis_grant (id, purpose, scope_mode,"
                " endpoint_identity, model_config_version, model_name,"
                " material_classes_json, manifest_hash, consent_version, created_at,"
                " revoked_at) VALUES ('g-new', 'reading_synthesis', 'workspace',"
                " 'http://127.0.0.1:9/v1', 'v-test', 'test-model',"
                " '[\"asset_content\"]', NULL, 'reading-analysis.consent/1', :at, NULL)"
            ),
            {"at": at.isoformat()},
        )
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_derived_copy (id, source_kind, source_id,"
                " source_revision, derived_kind, derived_id, field_kind, created_at)"
                " VALUES ('d-new', 'candidate', 'cand-1', NULL, 'report', 'report-1',"
                " 'synthesis', :at)"
            ),
            {"at": at.isoformat()},
        )

    _upgrade(sqlite_engine, "0031")

    def rows() -> tuple[list[tuple], list[tuple]]:
        with sqlite_engine.connect() as connection:
            grants = [
                tuple(row)
                for row in connection.execute(
                    text("SELECT id, purpose FROM reading_analysis_grant ORDER BY id")
                )
            ]
            copies = [
                tuple(row)
                for row in connection.execute(
                    text("SELECT id, source_kind, field_kind FROM reading_derived_copy ORDER BY id")
                )
            ]
        return grants, copies

    grants, copies = rows()
    assert grants == [("g-old", "reading_reflection")]
    assert copies == [("d-old", "asset", "title")]

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_analysis_grant (id, purpose, scope_mode,"
                " endpoint_identity, model_config_version, model_name,"
                " material_classes_json, manifest_hash, consent_version, created_at,"
                " revoked_at) VALUES ('g-new', 'reading_synthesis', 'once',"
                " 'http://127.0.0.1:9/v1', 'v-test', 'test-model',"
                " '[\"asset_content\"]', :hash, 'reading-analysis.consent/1', :at, NULL)"
            ),
            {"at": at.isoformat(), "hash": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO reading_derived_copy (id, source_kind, source_id,"
                " source_revision, derived_kind, derived_id, field_kind, created_at)"
                " VALUES ('d-new', 'candidate', 'cand-1', NULL, 'report', 'report-1',"
                " 'synthesis', :at)"
            ),
            {"at": at.isoformat()},
        )
    assert rows()[0] == [("g-new", "reading_synthesis"), ("g-old", "reading_reflection")]

    # 有新值时不猜测降级：明确拒绝，旧行保持不变。
    with pytest.raises(RuntimeError, match="0031"):
        _downgrade(sqlite_engine, "0030")
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM reading_analysis_grant WHERE id = 'g-new'"))
        connection.execute(text("DELETE FROM reading_derived_copy WHERE id = 'd-new'"))
    _downgrade(sqlite_engine, "0030")
    grants, copies = rows()
    assert grants == [("g-old", "reading_reflection")]
    assert copies == [("d-old", "asset", "title")]
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reading_analysis_grant (id, purpose, scope_mode,"
                " endpoint_identity, model_config_version, model_name,"
                " material_classes_json, manifest_hash, consent_version, created_at,"
                " revoked_at) VALUES ('g-again', 'reading_synthesis', 'workspace',"
                " 'http://127.0.0.1:9/v1', 'v-test', 'test-model',"
                " '[\"asset_content\"]', NULL, 'reading-analysis.consent/1', :at, NULL)"
            ),
            {"at": at.isoformat()},
        )
    _upgrade(sqlite_engine, "0031")
    assert rows() == (grants, copies)


def test_0032_memory_preserves_rows_and_guards_downgrade(sqlite_engine: Engine) -> None:
    _upgrade(sqlite_engine, "0031")
    at = NOW_MIGRATIONS.isoformat()
    insert_grant = text(
        "INSERT INTO reading_analysis_grant (id, purpose, scope_mode, endpoint_identity,"
        " model_config_version, material_classes_json, consent_version, created_at)"
        " VALUES (:id, :purpose, 'workspace', 'http://127.0.0.1:9/v1', 'v-test',"
        " '[\"structured_reasons\"]', 'reading-analysis.consent/1', :at)"
    )
    insert_copy = text(
        "INSERT INTO reading_derived_copy (id, source_kind, source_id, source_revision,"
        " derived_kind, derived_id, field_kind, created_at)"
        " VALUES (:id, 'asset', 'asset-old', 2, :target, 'target-old', :field, :at)"
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            insert_grant, {"id": "old-grant", "purpose": "reading_synthesis", "at": at}
        )
        connection.execute(
            insert_copy, {"id": "old-copy", "target": "report", "field": "synthesis", "at": at}
        )

    def snapshot():
        with sqlite_engine.connect() as connection:
            return tuple(
                [
                    tuple(row)
                    for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY id"))
                ]
                for name in ("reading_analysis_grant", "reading_derived_copy")
            )

    before = snapshot()
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            insert_grant, {"id": "new-grant", "purpose": "reading_profile_inference", "at": at}
        )
    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            insert_copy, {"id": "new-copy", "target": "memory", "field": "memory", "at": at}
        )
    _upgrade(sqlite_engine, "0032")
    assert snapshot() == before
    with sqlite_engine.begin() as connection:
        connection.execute(
            insert_grant, {"id": "new-grant", "purpose": "reading_profile_inference", "at": at}
        )
        connection.execute(
            insert_copy, {"id": "new-copy", "target": "memory", "field": "memory", "at": at}
        )
        connection.execute(
            text(
                "INSERT INTO reading_memory (id, group_key, created_at) "
                "VALUES ('mem', 'value:test', :at)"
            ),
            {"at": at},
        )
    with pytest.raises(RuntimeError, match="0032"):
        _downgrade(sqlite_engine, "0031")
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM reading_memory"))
    # Even an empty memory table cannot silently lose its grants and provenance.
    with pytest.raises(RuntimeError, match="0032"):
        _downgrade(sqlite_engine, "0031")
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM reading_analysis_grant WHERE id='new-grant'"))
        connection.execute(text("DELETE FROM reading_derived_copy WHERE id='new-copy'"))
    _downgrade(sqlite_engine, "0031")
    assert snapshot() == before
    _upgrade(sqlite_engine, "0032")
    assert snapshot() == before
