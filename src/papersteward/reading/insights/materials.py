"""分析材料清单：选材预览、清单落库、预览令牌与用户补充摘录。

清单只存材料 ID、版本锚点与内容哈希（``ref_hash``），不复制私人原文；
同一用途下相同选材（清单哈希一致）复用同一行。预览令牌短期有效，绑定
清单哈希与模型配置版本，提交前重新核对。摘录是用户补充的私人材料，
``(paper_id, content_hash)`` 唯一约束做幂等去重。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.errors import (
    ConflictError,
    InvalidRequestError,
    NotAuthorizedError,
    NotFoundError,
)
from papersteward.llm.runtime import CONFIG_KEY
from papersteward.reading.insights.contracts import (
    ANALYSIS_PURPOSE,
    PREVIEW_TTL_SECONDS,
    AnalysisManifestContent,
    AnalysisPreviewView,
    ExcerptInput,
    ExcerptView,
    ManifestCoverage,
    MaterialExclusion,
    MaterialKind,
    MaterialRef,
)
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import (
    ReadingAnalysisExcerptRepository,
    ReadingAnalysisManifestRepository,
    ReadingAnalysisPreviewRepository,
    ReadingStructuredFeedbackRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    RatingRow,
    ReadingAnalysisExcerptRow,
    ReadingAnalysisManifestRow,
    ReadingAnalysisPreviewRow,
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingStructuredFeedbackRow,
)
from papersteward.storage.repositories import ConfigRepository

__all__ = [
    "add_excerpt",
    "create_manifest",
    "create_preview_token",
    "delete_excerpt",
    "list_excerpts",
    "manifest_content_of",
    "preview_materials",
    "validate_preview_token",
]


def manifest_content_of(row: ReadingAnalysisManifestRow) -> AnalysisManifestContent:
    return AnalysisManifestContent.model_validate(row.content_json)


def preview_materials(
    session: Session,
    *,
    paper_ids: list[str],
    kinds: list[MaterialKind],
    exclusions: list[MaterialExclusion] | None = None,
    now: datetime | None = None,
    purpose: str = ANALYSIS_PURPOSE,
) -> AnalysisManifestContent:
    """按选材构建清单正文；纯只读，缺论文/未归档整体 400。"""
    built_at = as_utc(now) if now is not None else datetime.now(UTC)
    unique_ids = list(dict.fromkeys(paper_ids))
    snapshots = {
        row.paper_id: row
        for row in session.scalars(
            select(ArchiveSnapshotRow).where(ArchiveSnapshotRow.paper_id.in_(unique_ids))
        ).all()
    }
    missing = sorted(set(unique_ids) - set(snapshots))
    if missing:
        raise InvalidRequestError(
            "以下论文不存在或尚未归档，请核对后再选材",
            details={"paper_ids": missing},
        )
    exclusion_map = _exclusion_map(exclusions or [])
    selected_kinds = list(dict.fromkeys(kinds))
    materials: list[MaterialRef] = []
    for paper_id in unique_ids:
        row = snapshots[paper_id]
        materials.extend(
            _paper_materials(session, paper_id, row, selected_kinds, exclusion_map, built_at)
        )
    by_kind: dict[str, int] = {}
    for ref in materials:
        by_kind[ref.kind] = by_kind.get(ref.kind, 0) + 1
    covered = {ref.paper_id for ref in materials if ref.paper_id is not None}
    archived_total = session.scalar(select(func.count()).select_from(ArchiveSnapshotRow)) or 0
    return AnalysisManifestContent(
        purpose=purpose,
        built_at=built_at,
        materials=materials,
        exclusions=list(exclusions or []),
        coverage=ManifestCoverage(
            archived_total=int(archived_total),
            selected_papers=len(covered),
            by_kind=by_kind,
        ),
    )


def create_manifest(
    session: Session,
    *,
    paper_ids: list[str],
    kinds: list[MaterialKind],
    exclusions: list[MaterialExclusion] | None = None,
    now: datetime,
    purpose: str = ANALYSIS_PURPOSE,
) -> tuple[ReadingAnalysisManifestRow, bool]:
    """构建并落库清单；同用途同选材（哈希一致）复用既有行。调用方持写事务。"""
    content = preview_materials(
        session, paper_ids=paper_ids, kinds=kinds, exclusions=exclusions, now=now, purpose=purpose
    )
    manifest_hash = content_hash(content.model_dump(mode="json"))
    existing = ReadingAnalysisManifestRepository(session).by_hash(purpose, manifest_hash)
    if existing is not None:
        return existing, False
    row = ReadingAnalysisManifestRepository(session).add(
        ReadingAnalysisManifestRow(
            id=uuid4().hex,
            purpose=purpose,
            manifest_hash=manifest_hash,
            content_json=content.model_dump(mode="json"),
            created_at=as_utc(now),
        )
    )
    return row, True


def create_preview_token(
    session: Session, *, manifest_id: str, now: datetime, purpose: str = ANALYSIS_PURPOSE
) -> AnalysisPreviewView:
    """生成短期预览令牌：绑定清单哈希与当前模型配置版本。调用方持写事务。"""
    manifest = ReadingAnalysisManifestRepository(session).get(manifest_id)
    if manifest is None or manifest.purpose != purpose:
        raise NotFoundError("清单不存在")
    config_row = ConfigRepository(session).latest(CONFIG_KEY)
    if config_row is None:
        raise InvalidRequestError("尚未连接模型：预览令牌需要绑定当前模型配置")
    created = as_utc(now)
    row = ReadingAnalysisPreviewRepository(session).add(
        ReadingAnalysisPreviewRow(
            id=uuid4().hex,
            token=uuid4().hex,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            purpose=manifest.purpose,
            model_config_version=config_row.version,
            expires_at=created + timedelta(seconds=PREVIEW_TTL_SECONDS),
            consumed_at=None,
            created_at=created,
        )
    )
    return _preview_view(row)


def validate_preview_token(
    session: Session,
    *,
    token: str,
    purpose: str = ANALYSIS_PURPOSE,
    manifest_hash: str | None = None,
    now: datetime,
) -> ReadingAnalysisPreviewRow:
    """提交/读取前的令牌核对：匹配、未消费、未过期且模型配置未变化。"""
    row = ReadingAnalysisPreviewRepository(session).find_by_token(token)
    if row is None:
        raise NotFoundError("预览令牌不存在，请重新生成预览")
    if row.purpose != purpose or (manifest_hash is not None and row.manifest_hash != manifest_hash):
        raise InvalidRequestError("预览令牌与当前清单不匹配，请重新预览")
    if row.consumed_at is not None:
        raise ConflictError("预览令牌已用于一次提交，请重新预览后再提交")
    if as_utc(row.expires_at) < as_utc(now):
        raise NotAuthorizedError("预览令牌已过期，请重新生成预览后再提交")
    config_row = ConfigRepository(session).latest(CONFIG_KEY)
    if config_row is None or config_row.version != row.model_config_version:
        raise InvalidRequestError("模型连接已变化，请重新预览后再提交")
    return row


def consume_preview_token(
    session: Session, row: ReadingAnalysisPreviewRow, *, now: datetime
) -> None:
    """提交成功后标记消费；同一令牌不能提交两次。调用方持写事务。"""
    row.consumed_at = as_utc(now)
    session.flush()


def add_excerpt(
    session: Session, paper_id: str, *, data: ExcerptInput, now: datetime
) -> ExcerptView:
    """新增用户摘录；同论文同内容幂等返回既有行。调用方持写事务。"""
    if session.get(ArchiveSnapshotRow, paper_id) is None:
        raise NotFoundError("归档不存在")
    digest = content_hash({"content": data.content})
    repository = ReadingAnalysisExcerptRepository(session)
    existing = repository.find_by_hash(paper_id, digest)
    if existing is not None:
        return _excerpt_view(existing)
    row = repository.add(
        ReadingAnalysisExcerptRow(
            id=uuid4().hex,
            paper_id=paper_id,
            content=data.content,
            source_note=data.source_note,
            content_hash=digest,
            recorded_at=as_utc(now),
        )
    )
    return _excerpt_view(row)


def list_excerpts(session: Session, paper_id: str) -> list[ExcerptView]:
    return [
        _excerpt_view(row)
        for row in ReadingAnalysisExcerptRepository(session).list_for_paper(paper_id)
    ]


def delete_excerpt(session: Session, paper_id: str, excerpt_id: str) -> None:
    row = ReadingAnalysisExcerptRepository(session).get(excerpt_id)
    if row is None or row.paper_id != paper_id:
        raise NotFoundError("摘录不存在")
    ReadingAnalysisExcerptRepository(session).delete(excerpt_id)


def _paper_materials(
    session: Session,
    paper_id: str,
    row: ArchiveSnapshotRow,
    kinds: list[MaterialKind],
    exclusion_map: dict[tuple[str, str | None], MaterialExclusion],
    built_at: datetime,
) -> list[MaterialRef]:
    refs: list[MaterialRef] = []

    def excluded(kind: MaterialKind) -> bool:
        return (paper_id, None) in exclusion_map or (paper_id, kind) in exclusion_map

    content, _available = _verified(session, row)
    metadata = content.get("metadata") if isinstance(content.get("metadata"), dict) else {}
    model_output = {
        "summary": content.get("summary"),
        "score": content.get("score"),
    }
    rating = _latest_rating(session, paper_id, built_at)
    structured = _latest_structured(session, paper_id, built_at)
    assets = _assets_for_paper(session, paper_id)
    excerpts = ReadingAnalysisExcerptRepository(session).list_for_paper(paper_id)

    for kind in kinds:
        if excluded(kind):
            continue
        if kind == "archive_metadata":
            refs.append(
                MaterialRef(
                    kind=kind,
                    target_id=paper_id,
                    paper_id=paper_id,
                    version_key=row.content_hash,
                    fields=["metadata"],
                    ref_hash=content_hash({"metadata": metadata}),
                )
            )
        elif kind == "model_summary":
            refs.append(
                MaterialRef(
                    kind=kind,
                    target_id=paper_id,
                    paper_id=paper_id,
                    version_key=row.content_hash,
                    fields=["summary", "score"],
                    ref_hash=content_hash(model_output),
                )
            )
        elif kind == "user_rating":
            if rating is None:
                continue
            refs.append(
                MaterialRef(
                    kind=kind,
                    target_id=paper_id,
                    paper_id=paper_id,
                    version_key=as_utc(rating.created_at).isoformat(),
                    fields=["score"],
                    ref_hash=content_hash({"score": rating.score}),
                )
            )
        elif kind == "user_notes":
            if rating is None or not rating.note:
                continue
            refs.append(
                MaterialRef(
                    kind=kind,
                    target_id=paper_id,
                    paper_id=paper_id,
                    version_key=as_utc(rating.created_at).isoformat(),
                    fields=["note"],
                    ref_hash=content_hash({"note": rating.note}),
                )
            )
        elif kind == "structured_reasons":
            if structured is None:
                continue
            refs.append(
                MaterialRef(
                    kind=kind,
                    target_id=paper_id,
                    paper_id=paper_id,
                    version_key=f"revision:{structured.revision}",
                    fields=["value_sources", "limited_reasons", "flags"],
                    ref_hash=_structured_hash(structured),
                )
            )
        elif kind == "asset_content":
            for asset, revision in assets:
                refs.append(
                    MaterialRef(
                        kind=kind,
                        target_id=asset.id,
                        paper_id=paper_id,
                        version_key=f"revision:{revision.revision}",
                        fields=["title", "body", "applicability", "status"],
                        ref_hash=content_hash(
                            {
                                "title": revision.title,
                                "body": revision.body,
                                "applicability": revision.applicability,
                                "status": revision.status,
                            }
                        ),
                    )
                )
        elif kind == "user_excerpt":
            refs.extend(
                MaterialRef(
                    kind=kind,
                    target_id=excerpt.id,
                    paper_id=paper_id,
                    version_key=excerpt.content_hash,
                    fields=["content", "source_note"],
                    ref_hash=content_hash(
                        {"content": excerpt.content, "source_note": excerpt.source_note}
                    ),
                )
                for excerpt in excerpts
            )
    return refs


def _exclusion_map(
    exclusions: list[MaterialExclusion],
) -> dict[tuple[str, str | None], MaterialExclusion]:
    return {(item.paper_id, item.kind): item for item in exclusions}


def _verified(session: Session, row: ArchiveSnapshotRow) -> tuple[dict[str, Any], bool]:
    from papersteward.reading.archive import verified_archive_content

    try:
        return verified_archive_content(row.content_json, row.content_hash)
    except Exception:  # noqa: BLE001 - 清单对坏归档降级为空材料，与投影一致
        return {}, False


def _latest_rating(session: Session, paper_id: str, cutoff: datetime) -> RatingRow | None:
    rows = session.scalars(
        select(RatingRow)
        .where(RatingRow.paper_id == paper_id, RatingRow.created_at < cutoff)
        .order_by(RatingRow.created_at)
    ).all()
    return rows[-1] if rows else None


def _latest_structured(
    session: Session, paper_id: str, cutoff: datetime
) -> ReadingStructuredFeedbackRow | None:
    items = ReadingStructuredFeedbackRepository(session).list_for_paper(paper_id)
    visible = [item for item in items if as_utc(item.recorded_at) < cutoff]
    return visible[-1] if visible else None


def _assets_for_paper(
    session: Session, paper_id: str
) -> list[tuple[ReadingAssetRow, ReadingAssetRevisionRow]]:
    from papersteward.storage.insights import ReadingAssetRepository

    return ReadingAssetRepository(session).assets_for_paper(paper_id)


def _structured_hash(row: ReadingStructuredFeedbackRow) -> str:
    return content_hash(
        {
            "revision": row.revision,
            "value_sources": list(row.value_sources_json or []),
            "limited_reasons": list(row.limited_reasons_json or []),
            "has_texts": any(
                (
                    row.context_note,
                    row.applicability,
                    row.takeaway,
                    row.question,
                    row.usage_result,
                )
            ),
        }
    )


def _excerpt_view(row: ReadingAnalysisExcerptRow) -> ExcerptView:
    return ExcerptView(
        excerpt_id=row.id,
        paper_id=row.paper_id,
        content=row.content,
        source_note=row.source_note,
        content_hash=row.content_hash,
        recorded_at=as_utc(row.recorded_at),
    )


def _preview_view(row: ReadingAnalysisPreviewRow) -> AnalysisPreviewView:
    return AnalysisPreviewView(
        token=row.token,
        manifest_id=row.manifest_id,
        manifest_hash=row.manifest_hash,
        purpose=row.purpose,
        model_config_version=row.model_config_version,
        expires_at=as_utc(row.expires_at),
        consumed=row.consumed_at is not None,
    )
