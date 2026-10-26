from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from papersteward import __version__
from papersteward.api import create_app
from papersteward.cli.demo import DEMO_PAPER_ID, DEMO_PAPER_TITLE, seed_demo_data
from papersteward.cli.doctor import (
    CHECK_CONFIG,
    CHECK_DATABASE,
    CHECK_MIGRATION,
    DoctorCheck,
    DoctorReport,
    run_doctor,
)
from papersteward.cli.main import main
from papersteward.config import Settings, get_settings
from papersteward.storage import (
    PaperRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)


def _checks(report: DoctorReport) -> dict[str, DoctorCheck]:
    return {check.name: check for check in report.checks}


def test_doctor_passes_on_migrated_database(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    report = run_doctor(_settings(tmp_path), engine=migrated_engine)

    assert report.ok
    checks = _checks(report)
    assert checks[CHECK_CONFIG].ok is True
    assert checks[CHECK_DATABASE].detail == "ok"
    assert "current=0030" in checks[CHECK_MIGRATION].detail
    assert "head=0030" in checks[CHECK_MIGRATION].detail


def test_doctor_reports_pending_migration(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    report = run_doctor(_settings(tmp_path), engine=sqlite_engine)

    assert not report.ok
    migration = _checks(report)[CHECK_MIGRATION]
    assert migration.ok is False
    assert "current=none" in migration.detail
    assert "alembic upgrade head" in migration.detail


def test_doctor_reports_unavailable_database(tmp_path: Path) -> None:
    not_a_database = tmp_path / "not-a-database"
    not_a_database.mkdir()
    engine = create_db_engine(f"sqlite+pysqlite:///{not_a_database}")
    try:
        report = run_doctor(_settings(tmp_path), engine=engine)
    finally:
        engine.dispose()

    assert not report.ok
    checks = _checks(report)
    assert checks[CHECK_DATABASE].ok is False
    assert checks[CHECK_MIGRATION].ok is False


def test_doctor_reports_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "70000")
    get_settings.cache_clear()

    report = run_doctor()

    assert not report.ok
    assert len(report.checks) == 1
    check = report.checks[0]
    assert check.name == CHECK_CONFIG
    assert "PAPERSTEWARD_API_PORT" in check.detail


def test_doctor_reports_engine_creation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", "not-a-valid-url")
    get_settings.cache_clear()

    report = run_doctor()

    assert not report.ok
    checks = _checks(report)
    assert checks[CHECK_DATABASE].ok is False
    assert checks[CHECK_MIGRATION].ok is False


def test_seed_demo_data_is_idempotent(migrated_engine: Engine) -> None:
    first = seed_demo_data(engine=migrated_engine)
    second = seed_demo_data(engine=migrated_engine)

    assert first.paper_id == DEMO_PAPER_ID
    assert first.created is True
    assert first.total == 1
    assert second.created is False
    assert second.total == 1

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        paper = PaperRepository(session).get(DEMO_PAPER_ID)
    assert paper is not None
    assert paper.title == DEMO_PAPER_TITLE


def test_demo_seed_is_visible_through_api(migrated_engine: Engine) -> None:
    seed_demo_data(engine=migrated_engine)
    app = create_app(engine=migrated_engine)

    with TestClient(app) as client:
        response = client.get("/papers")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == DEMO_PAPER_ID
    assert body["items"][0]["title"] == DEMO_PAPER_TITLE


def test_doctor_cli_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrated_engine: Engine
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()

    code = main(["doctor"])
    captured = capsys.readouterr()

    assert code == 0
    assert "结果：通过" in captured.out
    assert "迁移版本" in captured.out
    assert "current=0030" in captured.out


def test_doctor_cli_fails_without_migration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sqlite_engine: Engine,
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(sqlite_engine.url))
    get_settings.cache_clear()

    code = main(["doctor"])
    captured = capsys.readouterr()

    assert code == 1
    assert "结果：失败" in captured.out
    assert "alembic upgrade head" in captured.out


def test_doctor_cli_reports_config_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "70000")
    get_settings.cache_clear()

    code = main(["doctor"])
    captured = capsys.readouterr()

    assert code == 1
    assert "结果：失败" in captured.out
    assert "PAPERSTEWARD_API_PORT" in captured.out
    assert "       - PAPERSTEWARD_API_PORT" in captured.out


def test_demo_seed_cli_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_engine: Engine,
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()

    first_code = main(["demo", "seed"])
    first = capsys.readouterr()
    second_code = main(["demo", "seed"])
    second = capsys.readouterr()

    assert first_code == 0
    assert "已插入演示文献 p001" in first.out
    assert second_code == 0
    assert "已存在" in second.out


def test_demo_seed_cli_fails_without_migration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sqlite_engine: Engine,
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(sqlite_engine.url))
    get_settings.cache_clear()

    code = main(["demo", "seed"])
    captured = capsys.readouterr()

    assert code == 1
    assert "alembic upgrade head" in captured.err
    assert inspect(sqlite_engine).get_table_names() == []


def test_demo_seed_cli_reports_config_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "70000")
    get_settings.cache_clear()

    code = main(["demo", "seed"])
    captured = capsys.readouterr()

    assert code == 1
    assert "PAPERSTEWARD_API_PORT" in captured.err
    assert "配置校验失败" in captured.err


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out
