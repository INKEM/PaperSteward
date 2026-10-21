"""文档相关 CLI：validate / import / confirm / archive / diff。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from papersteward.config import format_settings_error
from papersteward.core.entities import ResearchProfile, Taxonomy
from papersteward.core.errors import DomainError
from papersteward.documents import (
    DocumentError,
    archive_profile,
    archive_taxonomy,
    confirm_profile,
    confirm_taxonomy,
    diff_profiles,
    diff_taxonomies,
    import_profile,
    import_taxonomy,
    parse_profile,
    parse_taxonomy,
    render_diff,
)
from papersteward.storage import (
    ProfileRepository,
    TaxonomyRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)

DocumentKind = Literal["profile", "taxonomy"]
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


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _profile_summary(profile: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "profile",
        "id": profile.id,
        "version": profile.version,
        "status": profile.status.value,
        "core_questions": len(profile.parsed_json.get("core_questions", [])),
        "subdirections": len(profile.parsed_json.get("subdirections", [])),
    }


def _taxonomy_summary(taxonomy: Any) -> dict[str, Any]:
    primary = sum(1 for tag in taxonomy.tags if tag.level.value == "primary")
    secondary = len(taxonomy.tags) - primary
    return {
        "ok": True,
        "kind": "taxonomy",
        "id": taxonomy.id,
        "version": taxonomy.version,
        "status": taxonomy.status.value,
        "primary_tags": primary,
        "secondary_tags": secondary,
    }


def run_validate(kind: DocumentKind, path: str, *, json_output: bool) -> int:
    try:
        text = _read_text(path)
        summary = (
            _profile_summary(parse_profile(text, source=path))
            if kind == "profile"
            else _taxonomy_summary(parse_taxonomy(text, source=path))
        )
    except (DocumentError, DomainError, OSError) as exc:
        _print_error(exc)
        return 1

    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif kind == "profile":
        print(
            f"OK profile {summary['id']} v{summary['version']}: "
            f"{summary['core_questions']} core questions, "
            f"{summary['subdirections']} subdirections"
        )
    else:
        print(
            f"OK taxonomy {summary['id']} v{summary['version']}: "
            f"{summary['primary_tags']} primary tags, "
            f"{summary['secondary_tags']} secondary tags"
        )
    return 0


def run_import(kind: DocumentKind, path: str, *, document_id: str | None) -> int:
    document: ResearchProfile | Taxonomy
    try:
        with _session() as session:
            if kind == "profile":
                document = import_profile(session, path, document_id=document_id)
            else:
                document = import_taxonomy(session, path, document_id=document_id)
    except IntegrityError:
        _print_error(ValueError(f"{kind} 版本已存在；请提升 front matter 的 version"))
        return 1
    except (DocumentError, OSError, DomainError) as exc:
        _print_error(exc)
        return 1
    except ValidationError as exc:
        _print_settings_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1

    print(f"已导入 {kind} {document.id} v{document.version}（draft）")
    return 0


def _load_document(session: Session, kind: DocumentKind, document_id: str, version: str) -> Any:
    repository: Any = (
        ProfileRepository(session) if kind == "profile" else TaxonomyRepository(session)
    )
    document = repository.get(document_id, version)
    if document is None:
        raise ValueError(f"{kind} {document_id} v{version} 不存在")
    return document


def run_confirm(
    kind: DocumentKind,
    version: str,
    *,
    document_id: str,
    at: str | None,
) -> int:
    document: ResearchProfile | Taxonomy
    try:
        timestamp = _parse_time(at, "--at")
        with _session() as session:
            if kind == "profile":
                document = confirm_profile(session, document_id, version, confirmed_at=timestamp)
            else:
                document = confirm_taxonomy(session, document_id, version, confirmed_at=timestamp)
    except ValidationError as exc:
        _print_settings_error(exc)
        return 1
    except (DocumentError, OSError, DomainError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1

    print(f"已确认 {kind} {document.id} v{document.version}")
    return 0


def run_archive(
    kind: DocumentKind,
    version: str,
    *,
    document_id: str,
    at: str | None,
) -> int:
    document: ResearchProfile | Taxonomy
    try:
        timestamp = _parse_time(at, "--at")
        with _session() as session:
            if kind == "profile":
                document = archive_profile(session, document_id, version, archived_at=timestamp)
            else:
                document = archive_taxonomy(session, document_id, version, archived_at=timestamp)
    except ValidationError as exc:
        _print_settings_error(exc)
        return 1
    except (DocumentError, OSError, DomainError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1

    print(f"已归档 {kind} {document.id} v{document.version}")
    return 0


def _load_or_parse_pair(
    kind: DocumentKind,
    left: str,
    right: str,
    *,
    document_id: str | None,
) -> tuple[Any, Any]:
    left_path = Path(left)
    right_path = Path(right)
    left_is_file = left_path.is_file()
    right_is_file = right_path.is_file()
    if left_is_file and right_is_file:
        left_text = left_path.read_text(encoding="utf-8")
        right_text = right_path.read_text(encoding="utf-8")
        if kind == "profile":
            return (
                parse_profile(left_text, source=left),
                parse_profile(right_text, source=right),
            )
        return (
            parse_taxonomy(left_text, source=left),
            parse_taxonomy(right_text, source=right),
        )
    if left_is_file or right_is_file:
        raise ValueError("diff 的两个参数必须都是文件，或者都按数据库版本比较")
    if not document_id:
        raise ValueError(
            "文件不存在；若按数据库版本比较，请提供 --id，例如 "
            "`doc diff profile 1.0 1.1 --id demo-profile`"
        )
    with _session() as session:
        return (
            _load_document(session, kind, document_id, left),
            _load_document(session, kind, document_id, right),
        )


def run_diff(
    kind: DocumentKind,
    left: str,
    right: str,
    *,
    document_id: str | None,
    json_output: bool,
) -> int:
    try:
        left_document, right_document = _load_or_parse_pair(
            kind, left, right, document_id=document_id
        )
        entries = (
            diff_profiles(left_document, right_document)
            if kind == "profile"
            else diff_taxonomies(left_document, right_document)
        )
    except ValidationError as exc:
        _print_settings_error(exc)
        return 1
    except (DocumentError, OSError, DomainError, ValueError) as exc:
        _print_error(exc)
        return 1
    except SQLAlchemyError:
        _print_error(ValueError(_MIGRATION_HINT))
        return 1

    if json_output:
        payload = {
            "kind": kind,
            "left": {"id": left_document.id, "version": left_document.version},
            "right": {"id": right_document.id, "version": right_document.version},
            "entries": [
                {
                    "path": entry.path,
                    "change": entry.change,
                    "old": entry.old,
                    "new": entry.new,
                }
                for entry in entries
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{kind} {left_document.id}: {left_document.version} -> {right_document.version}")
        print(render_diff(entries))
    return 0


__all__ = [
    "DocumentKind",
    "run_archive",
    "run_confirm",
    "run_diff",
    "run_import",
    "run_validate",
]
