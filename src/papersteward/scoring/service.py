"""评分配置的版本化持久化与事件写入。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from papersteward.core.entities import Config, Event, utc_now
from papersteward.core.enums import AssignedBy, EventType
from papersteward.scoring.config import (
    ConfigError,
    ScoringConfig,
    config_value_json,
    load_scoring_config_file,
    validate_scoring_config,
)
from papersteward.storage.repositories import ConfigRepository, EventRepository

CONFIG_KEY = "scoring"


def _event_id(version: str) -> str:
    digest = hashlib.sha256(f"config:{CONFIG_KEY}:{version}".encode()).hexdigest()[:16]
    return f"evt-config-updated-{digest}"


def save_scoring_config(
    session: Session,
    config: ScoringConfig,
    *,
    updated_by: AssignedBy = AssignedBy.USER,
    updated_at: datetime | None = None,
) -> Config:
    """追加一个配置版本并写入 ``config.updated`` 事件；不 commit。"""
    timestamp = updated_at or utc_now()
    entity = Config(
        key=CONFIG_KEY,
        version=config.version,
        value_json=config_value_json(config),
        updated_by=updated_by,
        updated_at=timestamp,
    )
    ConfigRepository(session).add(entity)
    EventRepository(session).append(
        Event(
            id=_event_id(config.version),
            event_type=EventType.CONFIG_UPDATED,
            created_at=timestamp,
            payload_json={
                "key": CONFIG_KEY,
                "version": config.version,
                "updated_by": updated_by.value,
            },
        )
    )
    return entity


def import_scoring_config(
    session: Session,
    path: str | Path,
    *,
    updated_by: AssignedBy = AssignedBy.USER,
    updated_at: datetime | None = None,
) -> Config:
    return save_scoring_config(
        session,
        load_scoring_config_file(path),
        updated_by=updated_by,
        updated_at=updated_at,
    )


def read_scoring_config(session: Session, *, version: str | None = None) -> ScoringConfig | None:
    """读取并重新校验数据库中的配置版本。"""
    repository = ConfigRepository(session)
    config = (
        repository.get(CONFIG_KEY, version)
        if version is not None
        else repository.latest(CONFIG_KEY)
    )
    if config is None:
        return None
    source = f"config:{CONFIG_KEY}@{config.version}"
    try:
        parsed = ScoringConfig.model_validate(config.value_json)
    except ValidationError as exc:
        raise ConfigError(
            "数据库中的评分配置不符合当前 schema",
            source=source,
        ) from exc
    validate_scoring_config(parsed, source=source)
    if parsed.version != config.version:
        raise ConfigError(
            f"配置版本不一致：数据库为 {config.version}，内容为 {parsed.version}",
            source=source,
            field="scoring.version",
        )
    return parsed


__all__ = [
    "CONFIG_KEY",
    "import_scoring_config",
    "read_scoring_config",
    "save_scoring_config",
]
