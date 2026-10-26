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
        assert inspector.get_pk_constraint("reading_asset_revision")["constrained_columns"] == [
            "asset_id",
            "revision",
        ]
        current = MigrationContext.configure(connection).get_current_revision()
        assert current == "0026"


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
    """旧请求只恢复有证据的字段；无运行记录的主题不默认为全部，往返不改正文。"""
    from papersteward.core.entities import Run
    from papersteward.jobs.ui_flow import ReviewParams
    from papersteward.reading.insights.reports import (
        prepare_report,
        read_report,
        report_input_status,
    )
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
    with session_scope(factory) as session:
        prepared = prepare_report(
            session,
            ReviewParams(period="week", anchor_date=date(2026, 10, 22)),
            collected_at=NOW_MIGRATIONS,
        )
    frozen = prepared.content.model_dump(mode="json")
    for ref in frozen["assets"]:
        for field in ("revision", "source_paper_ids", "cross_topic"):
            ref.pop(field, None)
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
                    period_label=prepared.period.label,
                    period_start=prepared.period.start_date,
                    period_end=prepared.period.end_date_inclusive,
                    timezone=prepared.period.timezone,
                    review_mode="as_recorded",
                    knowledge_cutoff=prepared.knowledge_cutoff,
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
