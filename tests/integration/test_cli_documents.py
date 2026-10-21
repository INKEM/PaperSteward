from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.storage import (
    EventRepository,
    ProfileRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE_V1 = FIXTURES / "profiles" / "basic.md"
PROFILE_V2 = FIXTURES / "profiles" / "v2.md"
PROFILE_INVALID = FIXTURES / "profiles" / "invalid.md"
TAXONOMY_V1 = FIXTURES / "taxonomies" / "basic.md"


@pytest.fixture
def cli_database(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Iterator[Engine]:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        yield migrated_engine
    finally:
        get_settings.cache_clear()


def test_profile_validate_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["profile", "validate", str(PROFILE_V1)])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK profile demo-profile v1.0" in captured.out
    assert "3 core questions" in captured.out


def test_profile_validate_cli_reports_field_and_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["profile", "validate", str(PROFILE_INVALID)])
    captured = capsys.readouterr()

    assert code == 1
    assert "front_matter.core_questions" in captured.err
    assert "invalid.md" in captured.err


def test_taxonomy_validate_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["taxonomy", "validate", str(TAXONOMY_V1)])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK taxonomy demo-taxonomy v1.0" in captured.out
    assert "5 primary tags" in captured.out


def test_doc_diff_cli_for_files(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doc", "diff", "profile", str(PROFILE_V1), str(PROFILE_V2)])
    captured = capsys.readouterr()

    assert code == 0
    assert "profile demo-profile: 1.0 -> 1.1" in captured.out
    assert "~ core_questions.Q2.text" in captured.out


def test_doc_diff_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doc", "diff", "profile", str(PROFILE_V1), str(PROFILE_V2), "--json"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["left"]["version"] == "1.0"
    assert payload["right"]["version"] == "1.1"
    assert any(entry["path"] == "core_questions.Q2.text" for entry in payload["entries"])


def test_import_confirm_archive_cli(
    cli_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["profile", "import", str(PROFILE_V1)]) == 0
    assert (
        main(["profile", "confirm", "1.0", "--id", "demo-profile", "--at", "2026-10-21T09:00:00Z"])
        == 0
    )
    assert main(["profile", "import", str(PROFILE_V2)]) == 0
    assert (
        main(["profile", "confirm", "1.1", "--id", "demo-profile", "--at", "2026-10-22T09:00:00Z"])
        == 0
    )

    code = main(["doc", "diff", "profile", "1.0", "1.1", "--id", "demo-profile"])
    captured = capsys.readouterr()
    assert code == 0
    assert "1.0 -> 1.1" in captured.out

    assert (
        main(["profile", "archive", "1.1", "--id", "demo-profile", "--at", "2026-10-23T09:00:00Z"])
        == 0
    )

    factory = create_session_factory(cli_database)
    with session_scope(factory) as session:
        profiles = ProfileRepository(session)
        first = profiles.get("demo-profile", "1.0")
        second = profiles.get("demo-profile", "1.1")
        assert first is not None and first.status is DocumentStatus.ARCHIVED
        assert second is not None and second.status is DocumentStatus.ARCHIVED
        event_types = [event.event_type for event in EventRepository(session).recent(limit=10)]
        assert EventType.PROFILE_CONFIRMED in event_types
        assert EventType.PROFILE_ARCHIVED in event_types


def test_import_invalid_profile_cli_writes_nothing(
    cli_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["profile", "import", str(PROFILE_INVALID)])
    captured = capsys.readouterr()

    assert code == 1
    assert "core_questions" in captured.err

    factory = create_session_factory(cli_database)
    with session_scope(factory) as session:
        assert ProfileRepository(session).list() == []


def test_doc_diff_requires_files_or_id(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doc", "diff", "profile", "1.0", "1.1"])
    captured = capsys.readouterr()

    assert code == 1
    assert "--id" in captured.err


def test_taxonomy_import_and_confirm_cli(
    cli_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["taxonomy", "import", str(TAXONOMY_V1)]) == 0
    assert (
        main(
            [
                "taxonomy",
                "confirm",
                "1.0",
                "--id",
                "demo-taxonomy",
                "--at",
                "2026-10-21T09:00:00Z",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "已确认 taxonomy demo-taxonomy v1.0" in captured.out


@pytest.mark.parametrize(
    "argv",
    [
        ["profile", "import", str(PROFILE_V1)],
        ["profile", "confirm", "1.0", "--id", "demo-profile"],
        ["profile", "archive", "1.0", "--id", "demo-profile"],
        ["doc", "diff", "profile", "1.0", "1.1", "--id", "demo-profile"],
    ],
)
def test_document_db_commands_report_settings_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "70000")
    get_settings.cache_clear()
    try:
        code = main(argv)
    finally:
        get_settings.cache_clear()

    captured = capsys.readouterr()
    assert code == 1
    assert "PAPERSTEWARD_API_PORT" in captured.err
    assert "配置校验失败" in captured.err
    assert "Traceback" not in captured.err
