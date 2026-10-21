from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from papersteward.core.entities import Config
from papersteward.core.enums import AssignedBy, EventType
from papersteward.scoring import ConfigError, import_scoring_config, read_scoring_config
from papersteward.storage import (
    ConfigRepository,
    EventRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VALID = FIXTURES / "configs" / "scoring.yaml"
INVALID = FIXTURES / "configs" / "scoring_invalid.yaml"
T1 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def test_import_scoring_config_writes_version_and_event(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        entity = import_scoring_config(session, VALID, updated_by=AssignedBy.USER, updated_at=T1)
        assert entity.key == "scoring"
        assert entity.version == "1.0.0"
        assert entity.value_json["static"]["relevance"] == 0.65

        events = EventRepository(session).recent(limit=1)
        assert len(events) == 1
        assert events[0].event_type is EventType.CONFIG_UPDATED
        assert events[0].payload_json == {
            "key": "scoring",
            "version": "1.0.0",
            "updated_by": "user",
        }

    with session_scope(factory) as session:
        config = read_scoring_config(session)
        assert config is not None
        assert config.version == "1.0.0"
        assert config.static.relevance == pytest.approx(0.65)


def test_import_config_does_not_commit(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    session = factory()
    try:
        import_scoring_config(session, VALID, updated_at=T1)
        session.rollback()
    finally:
        session.close()

    with session_scope(factory) as session:
        assert ConfigRepository(session).versions("scoring") == []
        assert EventRepository(session).recent(limit=10) == []


def test_duplicate_config_version_is_rejected(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        import_scoring_config(session, VALID, updated_at=T1)

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        import_scoring_config(session, VALID, updated_at=T2)


def test_invalid_config_is_not_written(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        with pytest.raises(ConfigError):
            import_scoring_config(session, INVALID, updated_at=T1)
        assert ConfigRepository(session).versions("scoring") == []
        assert EventRepository(session).recent(limit=10) == []


def test_read_latest_and_specific_version(migrated_engine: Engine, tmp_path: Path) -> None:
    factory = create_session_factory(migrated_engine)
    v2_path = tmp_path / "scoring_v2.yaml"
    v2_path.write_text(
        VALID.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 1.1.0"),
        encoding="utf-8",
    )

    with session_scope(factory) as session:
        import_scoring_config(session, VALID, updated_at=T1)
        import_scoring_config(session, v2_path, updated_at=T2)

    with session_scope(factory) as session:
        latest = read_scoring_config(session)
        old = read_scoring_config(session, version="1.0.0")
        assert latest is not None and latest.version == "1.1.0"
        assert old is not None and old.version == "1.0.0"
        assert read_scoring_config(session, version="9.9") is None


def test_stored_config_is_revalidated_on_read(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        ConfigRepository(session).add(
            Config(
                key="scoring",
                version="broken",
                value_json={"version": "broken"},
                updated_at=T1,
            )
        )

    with session_scope(factory) as session, pytest.raises(ConfigError, match="schema"):
        read_scoring_config(session, version="broken")
