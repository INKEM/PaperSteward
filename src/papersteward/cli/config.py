"""配置与 JSON schema 相关 CLI。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from papersteward.config import format_settings_error
from papersteward.core.errors import DomainError
from papersteward.schemas import (
    SCHEMA_MODELS,
    SchemaError,
    schema_for,
    validate_json_text,
)
from papersteward.scoring import (
    ConfigError,
    import_scoring_config,
    load_scoring_config_file,
)
from papersteward.storage import create_db_engine, create_session_factory, session_scope

_MIGRATION_HINT = "数据库尚未迁移或不可用。请先运行 `alembic upgrade head`。"


@contextmanager
def _session() -> Iterator[Session]:
    engine = create_db_engine()
    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            yield session
    finally:
        engine.dispose()


def _print_error(exc: Exception) -> None:
    print(f"错误：{exc}", file=sys.stderr)


def _print_settings_error(exc: ValidationError) -> None:
    print(format_settings_error(exc), file=sys.stderr)


def _parse_time(value: str | None, option: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{option} 必须是 ISO 8601 时间，例如 2026-10-21T08:00:00Z") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{option} 必须带时区")
    return parsed.astimezone(UTC)


def run_config_validate(path: str, *, json_output: bool) -> int:
    try:
        config = load_scoring_config_file(path)
    except (ConfigError, OSError) as exc:
        _print_error(exc)
        return 1

    summary: dict[str, Any] = {
        "ok": True,
        "kind": "scoring_config",
        "version": config.version,
        "static_weight": config.static.weight,
        "dynamic_weight": config.dynamic.weight,
        "quasi_dynamic_weight": config.quasi_dynamic.weight,
    }
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"OK scoring config v{config.version} "
            f"(static={config.static.weight:g}, "
            f"dynamic={config.dynamic.weight:g}, "
            f"quasi_dynamic={config.quasi_dynamic.weight:g})"
        )
    return 0


def run_config_import(path: str, *, at: str | None) -> int:
    try:
        timestamp = _parse_time(at, "--at")
        with _session() as session:
            entity = import_scoring_config(session, path, updated_at=timestamp)
    except IntegrityError:
        _print_error(ValueError("配置版本已存在；请提升 scoring.yaml 的 version"))
        return 1
    except ValidationError as exc:
        _print_settings_error(exc)
        return 1
    except (ConfigError, OSError, DomainError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1

    print(f"已导入配置 {entity.key} v{entity.version}")
    return 0


def run_schema_show(name: str) -> int:
    try:
        schema = schema_for(name)
    except SchemaError as exc:
        _print_error(exc)
        return 1
    print(json.dumps(schema, ensure_ascii=False, indent=2))
    return 0


def run_schema_validate(name: str, path: str) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8")
        validate_json_text(name, text, source=path)
    except (SchemaError, OSError) as exc:
        _print_error(exc)
        return 1

    print(f"OK schema {name}: {path}")
    return 0


def schema_names() -> tuple[str, ...]:
    return tuple(sorted(SCHEMA_MODELS))


__all__ = [
    "run_config_import",
    "run_config_validate",
    "run_schema_show",
    "run_schema_validate",
    "schema_names",
]
