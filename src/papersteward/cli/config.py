"""配置与 JSON schema 相关 CLI。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from papersteward.config import format_settings_error
from papersteward.core.entities import ScoringChange, utc_now
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
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
    validate_scoring_config,
)
from papersteward.scoring.governance import (
    confirm_weight_change,
    count_changes,
    get_change,
    list_changes,
    preview_from_config,
    preview_from_overrides,
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


def _parse_override(text: str) -> tuple[str, Any]:
    path, separator, raw = text.partition("=")
    if not separator or not path.strip():
        raise ValueError("--set 格式必须为 PATH=VALUE，例如 static.relevance=0.60")
    path = path.strip()
    if path == "version" or path.startswith("version."):
        raise ValueError("版本号请使用 --version，不要在 --set 中修改")
    raw = raw.strip()
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return path, value


def _change_json(change: ScoringChange) -> dict[str, Any]:
    return {
        "id": change.id,
        "status": change.status.value,
        "base_version": change.base_version,
        "config_version": change.config_version,
        "preview_day": change.preview_day.isoformat(),
        "effective_on": change.effective_on.isoformat(),
        "preview_hash": change.preview_hash,
        "proposed": dict(change.proposed_json),
        "preview": dict(change.preview_json),
        "created_at": change.created_at.isoformat(),
        "confirmed_at": change.confirmed_at.isoformat() if change.confirmed_at else None,
        "confirmed_by": change.confirmed_by,
        "note": change.note,
    }


def _print_change(change: ScoringChange) -> None:
    print(
        f"{change.id} [{change.status.value}] {change.base_version} → "
        f"{change.config_version}，生效 {change.effective_on}"
    )
    print(f"预览 hash：{change.preview_hash}")
    preview = change.preview_json
    print(f"候选 {preview.get('total', 0)} 篇，名次变化 {preview.get('changed', 0)} 篇")
    for item in preview.get("items", [])[:20]:
        print(
            f"  #{item['old_rank']}→#{item['new_rank']} {item['paper_id']} "
            f"P {item['old_priority']:.1f}→{item['new_priority']:.1f} {item['title']}"
        )
    if change.status.value == "previewed":
        print(f'确认：papersteward config confirm {change.id} --by <user> --note "确认理由"')


def run_config_preview(
    *,
    path: str | None,
    overrides: list[str],
    version: str | None,
    at: date | None,
    effective_on: date | None,
    limit: int,
    json_output: bool,
) -> int:
    try:
        parsed = dict(_parse_override(item) for item in overrides)
        preview_day = at or utc_now().date()
        effective = effective_on or preview_day + timedelta(days=1)
        with _session() as session:
            if path is not None:
                proposed = load_scoring_config_file(path)
                if version is not None:
                    proposed = proposed.model_copy(update={"version": version})
                validate_scoring_config(proposed, source=f"config-preview:{path}")
                change = preview_from_config(
                    session,
                    proposed,
                    preview_day=preview_day,
                    effective_on=effective,
                    limit=limit,
                )
            else:
                change = preview_from_overrides(
                    session,
                    parsed,
                    version=version,
                    preview_day=preview_day,
                    effective_on=effective,
                    limit=limit,
                )
    except (ConfigError, ValidationError, AppError, DomainError) as exc:
        _print_error(exc)
        return 1
    except (OSError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1
    if json_output:
        print(json.dumps(_change_json(change), ensure_ascii=False, indent=2))
    else:
        _print_change(change)
    return 0


def run_config_confirm(change_id: str, *, by: str, note: str | None, json_output: bool) -> int:
    try:
        with _session() as session:
            change, recomputed = confirm_weight_change(
                session, change_id, confirmed_by=by, note=note
            )
    except (ConfigError, ValidationError, AppError, DomainError) as exc:
        _print_error(exc)
        return 1
    except (OSError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1
    if json_output:
        payload = _change_json(change)
        payload["static_recomputed"] = recomputed
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"已激活 {change.config_version}（生效 {change.effective_on}，"
            f"静态快照重算 {recomputed} 篇）"
        )
    return 0


def run_config_changes(*, limit: int, json_output: bool) -> int:
    try:
        with _session() as session:
            changes = list_changes(session, limit=limit)
            total = count_changes(session)
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1
    if json_output:
        print(
            json.dumps(
                {
                    "items": [_change_json(item) for item in changes],
                    "total": total,
                    "limit": limit,
                    "offset": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for change in changes:
            print(
                f"{change.id} [{change.status.value}] {change.base_version} → "
                f"{change.config_version}，生效 {change.effective_on}"
            )
    return 0


def run_config_show(change_id: str, *, json_output: bool) -> int:
    try:
        with _session() as session:
            change = get_change(session, change_id)
    except (AppError, DomainError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1
    if json_output:
        print(json.dumps(_change_json(change), ensure_ascii=False, indent=2))
    else:
        _print_change(change)
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
    "run_config_changes",
    "run_config_confirm",
    "run_config_import",
    "run_config_preview",
    "run_config_show",
    "run_config_validate",
    "run_schema_show",
    "run_schema_validate",
    "schema_names",
]
