"""文档导入、确认与归档服务。

解析失败不写数据库；确认/归档通过领域状态机校验，并在同一事务追加事件。
服务不 commit，调用方使用 ``session_scope`` 控制事务。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from papersteward.core.entities import Event, ResearchProfile, Taxonomy, utc_now
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.core.errors import DomainValidationError
from papersteward.documents.profile import parse_profile
from papersteward.documents.taxonomy import parse_taxonomy
from papersteward.storage.repositories import (
    EventRepository,
    ProfileRepository,
    TaxonomyRepository,
)

DocumentKind = Literal["profile", "taxonomy"]


def _read(path: str | Path) -> tuple[str, str]:
    source = Path(path)
    return source.read_text(encoding="utf-8"), str(source)


def import_profile(
    session: Session, path: str | Path, *, document_id: str | None = None
) -> ResearchProfile:
    """解析 profile 文件并写入 draft；重复版本由 DB 主键拒绝。"""
    text, source = _read(path)
    profile = parse_profile(text, source=source, document_id=document_id)
    ProfileRepository(session).add(profile)
    return profile


def import_taxonomy(
    session: Session, path: str | Path, *, document_id: str | None = None
) -> Taxonomy:
    """解析 taxonomy 文件并写入 draft；标签随分类法版本写入。"""
    text, source = _read(path)
    taxonomy = parse_taxonomy(text, source=source, document_id=document_id)
    TaxonomyRepository(session).add(taxonomy)
    return taxonomy


def _event_id(kind: DocumentKind, action: str, document_id: str, version: str) -> str:
    digest = hashlib.sha256(f"{kind}:{action}:{document_id}:{version}".encode()).hexdigest()[:16]
    return f"evt-{kind}-{action}-{digest}"


def _document_event(
    *,
    kind: DocumentKind,
    action: str,
    document_id: str,
    version: str,
    event_type: EventType,
    created_at: datetime,
    extra: dict[str, Any] | None = None,
) -> Event:
    payload: dict[str, Any] = {
        "document_id": document_id,
        "version": version,
        "status": action,
    }
    if extra:
        payload.update(extra)
    return Event(
        id=_event_id(kind, action, document_id, version),
        event_type=event_type,
        created_at=created_at,
        payload_json=payload,
    )


def confirm_profile(
    session: Session,
    profile_id: str,
    version: str,
    *,
    confirmed_at: datetime | None = None,
) -> ResearchProfile:
    """确认 profile；若存在旧确认版本则先归档并追加事件。"""
    repository = ProfileRepository(session)
    events = EventRepository(session)
    if repository.get(profile_id, version) is None:
        raise DomainValidationError("profile", f"档案 {profile_id} v{version} 不存在")
    timestamp = confirmed_at or utc_now()

    previous = repository.latest_confirmed(profile_id)
    if previous is not None and previous.version != version:
        if repository.update_status(profile_id, previous.version, DocumentStatus.ARCHIVED) is None:
            raise DomainValidationError(
                "profile", f"旧版本 {profile_id} v{previous.version} 归档失败"
            )
        events.append(
            _document_event(
                kind="profile",
                action="archived",
                document_id=profile_id,
                version=previous.version,
                event_type=EventType.PROFILE_ARCHIVED,
                created_at=timestamp,
                extra={"superseded_by": version},
            )
        )

    updated = repository.update_status(
        profile_id,
        version,
        DocumentStatus.CONFIRMED,
        confirmed_at=timestamp,
    )
    if updated is None:
        raise DomainValidationError("profile", f"档案 {profile_id} v{version} 不存在")
    events.append(
        _document_event(
            kind="profile",
            action="confirmed",
            document_id=profile_id,
            version=version,
            event_type=EventType.PROFILE_CONFIRMED,
            created_at=timestamp,
        )
    )
    return updated


def confirm_taxonomy(
    session: Session,
    taxonomy_id: str,
    version: str,
    *,
    confirmed_at: datetime | None = None,
) -> Taxonomy:
    """确认 taxonomy；若存在旧确认版本则先归档并追加事件。"""
    repository = TaxonomyRepository(session)
    events = EventRepository(session)
    if repository.get(taxonomy_id, version) is None:
        raise DomainValidationError("taxonomy", f"分类法 {taxonomy_id} v{version} 不存在")
    timestamp = confirmed_at or utc_now()

    previous = repository.latest_confirmed(taxonomy_id)
    if previous is not None and previous.version != version:
        if repository.update_status(taxonomy_id, previous.version, DocumentStatus.ARCHIVED) is None:
            raise DomainValidationError(
                "taxonomy", f"旧版本 {taxonomy_id} v{previous.version} 归档失败"
            )
        events.append(
            _document_event(
                kind="taxonomy",
                action="archived",
                document_id=taxonomy_id,
                version=previous.version,
                event_type=EventType.TAXONOMY_ARCHIVED,
                created_at=timestamp,
                extra={"superseded_by": version},
            )
        )

    updated = repository.update_status(
        taxonomy_id,
        version,
        DocumentStatus.CONFIRMED,
        confirmed_at=timestamp,
    )
    if updated is None:
        raise DomainValidationError("taxonomy", f"分类法 {taxonomy_id} v{version} 不存在")
    events.append(
        _document_event(
            kind="taxonomy",
            action="confirmed",
            document_id=taxonomy_id,
            version=version,
            event_type=EventType.TAXONOMY_CONFIRMED,
            created_at=timestamp,
        )
    )
    return updated


def archive_profile(
    session: Session,
    profile_id: str,
    version: str,
    *,
    archived_at: datetime | None = None,
) -> ResearchProfile:
    """手动归档已确认的 profile；draft 不能直接归档。"""
    repository = ProfileRepository(session)
    timestamp = archived_at or utc_now()
    updated = repository.update_status(profile_id, version, DocumentStatus.ARCHIVED)
    if updated is None:
        raise DomainValidationError("profile", f"档案 {profile_id} v{version} 不存在")
    EventRepository(session).append(
        _document_event(
            kind="profile",
            action="archived",
            document_id=profile_id,
            version=version,
            event_type=EventType.PROFILE_ARCHIVED,
            created_at=timestamp,
            extra={"reason": "manual"},
        )
    )
    return updated


def archive_taxonomy(
    session: Session,
    taxonomy_id: str,
    version: str,
    *,
    archived_at: datetime | None = None,
) -> Taxonomy:
    """手动归档已确认的 taxonomy。"""
    repository = TaxonomyRepository(session)
    timestamp = archived_at or utc_now()
    updated = repository.update_status(taxonomy_id, version, DocumentStatus.ARCHIVED)
    if updated is None:
        raise DomainValidationError("taxonomy", f"分类法 {taxonomy_id} v{version} 不存在")
    EventRepository(session).append(
        _document_event(
            kind="taxonomy",
            action="archived",
            document_id=taxonomy_id,
            version=version,
            event_type=EventType.TAXONOMY_ARCHIVED,
            created_at=timestamp,
            extra={"reason": "manual"},
        )
    )
    return updated


__all__ = [
    "DocumentKind",
    "archive_profile",
    "archive_taxonomy",
    "confirm_profile",
    "confirm_taxonomy",
    "import_profile",
    "import_taxonomy",
]
