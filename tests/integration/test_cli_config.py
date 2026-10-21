from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import EventType
from papersteward.storage import (
    ConfigRepository,
    EventRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VALID = FIXTURES / "configs" / "scoring.yaml"
INVALID = FIXTURES / "configs" / "scoring_invalid.yaml"
INVALID_SUM = FIXTURES / "configs" / "scoring_invalid_sum.yaml"


@pytest.fixture
def cli_database(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Iterator[Engine]:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        yield migrated_engine
    finally:
        get_settings.cache_clear()


def _classification_json() -> dict[str, object]:
    return {
        "primary_tag": "T02",
        "primary_confidence": 0.86,
        "secondary_tags": [{"facet": "method", "name": "sparse retrieval", "confidence": 0.92}],
        "special_candidates": [],
        "boundary_note": None,
    }


def test_config_validate_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["config", "validate", str(VALID)])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK scoring config v1.0.0" in captured.out


def test_config_validate_cli_reports_field(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["config", "validate", str(INVALID)])
    captured = capsys.readouterr()

    assert code == 1
    assert "scoring.static.relevance" in captured.err
    assert "0.50" in captured.err

    code = main(["config", "validate", str(INVALID_SUM)])
    captured = capsys.readouterr()
    assert code == 1
    assert "scoring.dynamic" in captured.err


def test_config_import_cli_writes_version_and_event(
    cli_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["config", "import", str(VALID), "--at", "2026-10-21T09:00:00Z"])
    captured = capsys.readouterr()

    assert code == 0
    assert "已导入配置 scoring v1.0.0" in captured.out

    factory = create_session_factory(cli_database)
    with session_scope(factory) as session:
        config = ConfigRepository(session).get("scoring", "1.0.0")
        assert config is not None
        assert config.value_json["version"] == "1.0.0"
        events = EventRepository(session).recent(limit=1)
        assert events[0].event_type is EventType.CONFIG_UPDATED

    code = main(["config", "import", str(VALID), "--at", "2026-10-22T09:00:00Z"])
    captured = capsys.readouterr()
    assert code == 1
    assert "版本已存在" in captured.err


def test_config_import_cli_reports_settings_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "70000")
    get_settings.cache_clear()
    try:
        code = main(["config", "import", str(VALID)])
    finally:
        get_settings.cache_clear()

    captured = capsys.readouterr()
    assert code == 1
    assert "PAPERSTEWARD_API_PORT" in captured.err
    assert "Traceback" not in captured.err


def test_schema_show_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["schema", "show", "classification"])
    captured = capsys.readouterr()

    assert code == 0
    schema = json.loads(captured.out)
    assert schema["additionalProperties"] is False
    assert "primary_tag" in schema["properties"]


def test_schema_validate_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    valid = tmp_path / "classification.json"
    valid.write_text(json.dumps(_classification_json()), encoding="utf-8")
    assert main(["schema", "validate", "classification", str(valid)]) == 0
    assert "OK schema classification" in capsys.readouterr().out

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps({"primary_tag": "T02", "primary_confidence": 2}),
        encoding="utf-8",
    )
    assert main(["schema", "validate", "classification", str(invalid)]) == 1
    captured = capsys.readouterr()
    assert "schema.classification.primary_confidence" in captured.err

    invalid.write_text("{bad-json", encoding="utf-8")
    assert main(["schema", "validate", "classification", str(invalid)]) == 1
    assert "JSON 解析失败" in capsys.readouterr().err
