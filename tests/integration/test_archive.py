from copy import deepcopy
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from test_digest_delivery import prepared
from test_priority_queue import NOW

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import PaperStatus
from papersteward.delivery.contracts import Action
from papersteward.reading.actions import apply_action
from papersteward.reading.archive import read_archive
from papersteward.storage import session_scope
from papersteward.storage.models import ArchiveSnapshotRow, PaperRow
from papersteward.storage.repositories import (
    ArchiveSnapshotRepository,
    PaperRepository,
    RatingRepository,
)


def finish(session):
    return apply_action(
        session,
        "p",
        Action(request_id="done", kind="done", mode="deep", score=4, note="初始备注"),
        now=NOW,
    )


def test_done_archive_complete_and_historical_context_immutable(client, migrated_engine):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        assert finish(session)["status"] == "archived"
        record = read_archive(session, "p")
        assert record.content_available
        assert record.content["summary"]["evidence"] == ["evidence"]
        assert record.content["score"]["score_date"] == "2026-10-22"
        assert record.content["score"]["scores"]["P"] > 0
        assert record.content["score"]["config_version"] == "1.0.0"
        assert record.content["reading"]["actual_mode"] == "deep"
        assert record.content["reading"]["suggested_mode"] == "skim"
        assert record.content["reading"]["overridden"] is True
        assert record.primary_tag == "T01" and record.filing_bucket_id == "b-t01"
        saved = deepcopy(record.content)
        session.get(PaperRow, "p").title = "later title"
        apply_action(
            session,
            "p",
            Action(request_id="note", kind="note", note="新备注"),
            now=NOW + timedelta(seconds=1),
        )
        apply_action(
            session,
            "p",
            Action(request_id="score", kind="score", score=None),
            now=NOW + timedelta(seconds=2),
        )
        latest = read_archive(session, "p")
        assert latest.content == saved
        assert latest.initial_note == "初始备注" and latest.initial_score == 4
        assert latest.note == "新备注" and latest.score is None
        assert len(latest.revision_history) == 3
        assert ArchiveSnapshotRepository(session).get("p").content_hash == record.content_hash


def test_archive_failure_rolls_back_done_rating_and_state(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        with monkeypatch.context() as m:

            def broken(*args, **kwargs):
                raise RuntimeError("snapshot failed")

            m.setattr(ArchiveSnapshotRepository, "add", broken)
            with pytest.raises(RuntimeError):
                finish(session)
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED
        assert not RatingRepository(session).list_for_paper("p")
        assert session.get(ArchiveSnapshotRow, "p") is None


def test_archive_api_filter_export_and_no_delete(client, migrated_engine, monkeypatch):
    assert client.get("/archive").status_code == 409
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW)
    with session_scope(factory) as session:
        finish(session)
    assert client.get("/archive?tag=T01&min_score=4&query=初始").json()["total"] == 1
    assert client.get("/archive?tag=T02").json()["total"] == 0
    assert client.get("/archive?min_score=5").json()["total"] == 0
    assert client.get("/archive/p").json()["initial_note"] == "初始备注"
    assert client.get("/archive/missing").status_code == 404
    for format in ("bibtex", "csv", "markdown", "json"):
        response = client.get(f"/archive/export?format={format}")
        assert response.status_code == 200
        assert "T01" in response.text and "初始备注" in response.text
    assert client.delete("/archive/p").status_code == 405


def test_archive_cli_list_export_and_protect_existing_output(
    client, migrated_engine, tmp_path, monkeypatch, capsys
):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        finish(session)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        assert main(["archive", "list", "--json"]) == 0
        assert '"total": 1' in capsys.readouterr().out
        path = tmp_path / "archive.bib"
        assert main(["archive", "export", "--format", "bibtex", "--out", str(path)]) == 0
        assert "@article" in path.read_text() and "初始备注" in path.read_text()
        path.write_text("keep")
        assert main(["archive", "export", "--format", "bibtex", "--out", str(path)]) == 1
        assert path.read_text() == "keep"
    finally:
        get_settings.cache_clear()


def test_archive_content_migration_roundtrip_preserves_initial_feedback(client, migrated_engine):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        finish(session)
    config = Config("alembic.ini")
    with migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0009")
        assert tuple(
            connection.exec_driver_sql(
                "SELECT rating, note FROM archive_snapshot WHERE paper_id='p'"
            ).one()
        ) == (4, "初始备注")
        command.upgrade(config, "head")
    with session_scope(factory) as session:
        record = read_archive(session, "p")
        assert record.initial_score == 4 and record.initial_note == "初始备注"
        assert record.content_available is False  # 回滚删除完整内容，不用当前元数据伪造。
        assert PaperRepository(session).get("p").status is PaperStatus.ARCHIVED
