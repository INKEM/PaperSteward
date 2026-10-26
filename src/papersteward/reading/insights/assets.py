"""手工资产（收获/问题）的创建、修订、状态与删除；只引用已读归档。

语义（阶段计划 §5.3/§6 R1-05）：
- 版本只追加；每次修订提交全量内容 + ``expected_revision`` 乐观并发，
  来源集合随版本整体替换，旧版本与当时来源原样保留。
- 来源只信服务端：逐个校验归档快照存在并复制其内容哈希；
  客户端传来的任何来源内容或确认状态不采信。
- 幂等键相同提交重试不重复创建；kind 对应的合法状态在服务层校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    AssetRevisionInput,
    AssetRevisionSummary,
    AssetSourceInput,
    AssetSourceRef,
    AssetView,
)
from papersteward.reading.insights.reports import delete_report, list_reports, read_report
from papersteward.storage.insights import ReadingAssetRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    RatingRow,
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
)

_KIND_DEFAULT_STATUS = {"harvest": "active", "problem": "open"}
_KIND_ALLOWED_STATUS = {
    "harvest": {"active", "archived"},
    "problem": {"open", "resolved", "archived"},
}
_KIND_LABELS = {"harvest": "收获", "problem": "问题"}


def create_asset(session: Session, spec: AssetCreateInput, *, now: datetime) -> AssetView:
    """创建资产；幂等键相同重放原视图，不同内容拒绝。"""
    repository = ReadingAssetRepository(session)
    existing = repository.get_by_idempotency(spec.idempotency_key)
    if existing is not None:
        if existing.kind != spec.kind:
            raise InvariantViolation("asset.request_id", "请求ID已用于不同类型的资产")
        return get_asset_view(session, existing.id)
    resolved = _resolve_sources(session, spec.sources)
    asset_id = uuid4().hex
    repository.create(
        ReadingAssetRow(
            id=asset_id,
            kind=spec.kind,
            idempotency_key=spec.idempotency_key,
            created_at=now,
        ),
        ReadingAssetRevisionRow(
            asset_id=asset_id,
            revision=1,
            recorded_at=now,
            title=spec.title,
            body=spec.body,
            applicability=spec.applicability,
            status=_KIND_DEFAULT_STATUS[spec.kind],
        ),
        _source_rows(asset_id, 1, resolved),
    )
    return get_asset_view(session, asset_id)


def append_revision(
    session: Session, asset_id: str, spec: AssetRevisionInput, *, now: datetime
) -> AssetView:
    """追加全量内容版本；expected_revision 不一致即冲突。"""
    repository = ReadingAssetRepository(session)
    asset = repository.get_asset(asset_id)
    if asset is None:
        raise NotFoundError("资产不存在")
    current = repository.max_revision(asset_id)
    if spec.expected_revision != current:
        raise ConflictError(
            "资产已被其他页面更新，请基于最新版本重试",
            details={"current_revision": current},
        )
    _check_status(asset.kind, spec.status)
    resolved = _resolve_sources(session, spec.sources)
    repository.add_revision(
        ReadingAssetRevisionRow(
            asset_id=asset_id,
            revision=current + 1,
            recorded_at=now,
            title=spec.title,
            body=spec.body,
            applicability=spec.applicability,
            status=spec.status,
        ),
        _source_rows(asset_id, current + 1, resolved),
    )
    return get_asset_view(session, asset_id)


def change_status(
    session: Session,
    asset_id: str,
    status: str,
    *,
    now: datetime,
    expected_revision: int,
) -> AssetView:
    """只改状态的便捷修订（问题解决/重开、归档）；内容与来源沿用当前版本。"""
    view = get_asset_view(session, asset_id)
    current = view.revision
    spec = AssetRevisionInput.model_validate(
        {
            "expected_revision": expected_revision,
            "title": current.title,
            "body": current.body,
            "applicability": current.applicability,
            "status": status,
            "sources": [
                {"paper_id": source.paper_id, "rating_at": source.rating_at}
                for source in current.sources
            ],
        }
    )
    return append_revision(session, asset_id, spec, now=now)


def get_asset_view(session: Session, asset_id: str) -> AssetView:
    repository = ReadingAssetRepository(session)
    asset = repository.get_asset(asset_id)
    if asset is None:
        raise NotFoundError("资产不存在")
    revision_number = repository.max_revision(asset_id)
    revision_row = repository.revision(asset_id, revision_number)
    assert revision_row is not None
    return AssetView(
        asset_id=asset.id,
        kind=asset.kind,
        created_at=as_utc(asset.created_at),
        revision=_revision_view(repository, revision_row),
        history=[
            AssetRevisionSummary(
                revision=row.revision,
                recorded_at=as_utc(row.recorded_at),
                title=row.title,
                status=row.status,
            )
            for row in repository.revisions(asset_id)
        ],
    )


def list_assets(
    session: Session,
    *,
    kind: str | None = None,
    status: str | None = None,
    paper_id: str | None = None,
    keyword: str | None = None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    repository = ReadingAssetRepository(session)
    pairs, total = repository.list_current(
        kind=kind,
        status=status,
        paper_id=paper_id,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    items = [
        {
            "asset_id": asset.id,
            "kind": asset.kind,
            "revision": _revision_view(repository, revision),
        }
        for asset, revision in pairs
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def assets_for_paper(session: Session, paper_id: str) -> list[AssetView]:
    """引用了某篇论文（当前版本来源）的资产，按整理时间倒序。"""
    repository = ReadingAssetRepository(session)
    return [get_asset_view(session, asset.id) for asset, _ in repository.assets_for_paper(paper_id)]


def delete_preview(session: Session, asset_id: str) -> dict[str, Any]:
    """删除范围预览：版本数、来源引用数、曾选入正文副本的报告清单。"""
    repository = ReadingAssetRepository(session)
    if repository.get_asset(asset_id) is None:
        raise NotFoundError("资产不存在")
    revisions = repository.revisions(asset_id)
    source_count = sum(len(repository.sources(asset_id, row.revision)) for row in revisions)
    with_body, referenced = _reports_with_asset(session, asset_id)
    return {
        "revision_count": len(revisions),
        "source_count": source_count,
        "reports_with_body": with_body,
        "referenced_reports": referenced,
    }


def delete_asset(
    session: Session, asset_id: str, *, also_report_ids: Sequence[str] = ()
) -> list[str]:
    """删除资产；勾选的报告快照一并删除（清掉私人正文副本）。"""
    repository = ReadingAssetRepository(session)
    if repository.get_asset(asset_id) is None:
        raise NotFoundError("资产不存在")
    preview = delete_preview(session, asset_id)
    removable = set(preview["reports_with_body"])
    removed: list[str] = []
    for report_id in also_report_ids:
        if report_id not in removable:
            raise InvalidRequestError(
                "该报告不含此资产的正文副本，无需一并删除",
                details={"field": "also_report_ids"},
            )
        delete_report(session, report_id)
        removed.append(report_id)
    if not repository.delete_asset(asset_id):
        raise NotFoundError("资产不存在")
    return removed


def asset_events(session: Session, *, until: datetime) -> list[tuple[str, datetime]]:
    """截止前的全部资产版本事件；每个版本=一次整理操作。"""
    repository = ReadingAssetRepository(session)
    return [
        (row.asset_id, as_utc(row.recorded_at))
        for row in repository.all_revisions()
        if as_utc(row.recorded_at) < until
    ]


def _reports_with_asset(session: Session, asset_id: str) -> tuple[list[str], list[str]]:
    """含正文副本的报告与仅含引用的报告（按生成时间倒序）。"""
    summaries = []
    offset = 0
    while True:
        listing = list_reports(session, limit=200, offset=offset)
        summaries.extend(listing["items"])
        offset += len(listing["items"])
        if offset >= listing["total"]:
            break
    with_body: list[str] = []
    referenced: list[str] = []
    for summary in summaries:
        detail = read_report(session, summary.report_id)
        refs = detail.content.assets
        if asset_id not in {ref.asset_id for ref in refs}:
            continue
        if any(ref.asset_id == asset_id and ref.body is not None for ref in refs):
            with_body.append(detail.report_id)
        else:
            referenced.append(detail.report_id)
    return with_body, referenced


def _resolve_sources(
    session: Session, sources: Sequence[AssetSourceInput]
) -> list[tuple[str, str, datetime | None]]:
    """来源只信服务端：校验归档存在并复制其内容哈希。"""
    if not sources:
        raise DomainValidationError("asset.sources", "至少关联一篇已读文献")
    resolved: list[tuple[str, str, datetime | None]] = []
    seen: set[str] = set()
    for source in sources:
        if source.paper_id in seen:
            continue
        seen.add(source.paper_id)
        snapshot = session.get(ArchiveSnapshotRow, source.paper_id)
        if snapshot is None:
            raise DomainValidationError("asset.sources", f"来源 {source.paper_id} 不是已归档的文献")
        if (
            source.rating_at is not None
            and session.get(
                RatingRow, {"paper_id": source.paper_id, "created_at": source.rating_at}
            )
            is None
        ):
            raise DomainValidationError("asset.sources", "引用的反馈版本不存在，请重新选择")
        resolved.append(
            (
                source.paper_id,
                snapshot.content_hash,
                as_utc(source.rating_at) if source.rating_at else None,
            )
        )
    return resolved


def _source_rows(
    asset_id: str, revision: int, resolved: Sequence[tuple[str, str, datetime | None]]
) -> list[ReadingAssetSourceRow]:
    return [
        ReadingAssetSourceRow(
            id=uuid4().hex,
            asset_id=asset_id,
            revision=revision,
            paper_id=paper_id,
            archive_hash=archive_hash,
            rating_at=rating_at,
        )
        for paper_id, archive_hash, rating_at in resolved
    ]


def _check_status(kind: str, status: str) -> None:
    if status not in _KIND_ALLOWED_STATUS.get(kind, set()):
        raise DomainValidationError(
            "asset.status",
            f"{_KIND_LABELS.get(kind, kind)} 不支持状态 {status}",
        )


def _revision_view(repository: ReadingAssetRepository, row: ReadingAssetRevisionRow) -> Any:
    """当前版本视图；来源按 (asset_id, revision) 取当时集合。"""
    return {
        "revision": row.revision,
        "recorded_at": as_utc(row.recorded_at),
        "title": row.title,
        "body": row.body,
        "applicability": row.applicability,
        "status": row.status,
        "sources": [
            AssetSourceRef(
                paper_id=source.paper_id,
                archive_hash=source.archive_hash,
                rating_at=as_utc(source.rating_at) if source.rating_at else None,
            )
            for source in repository.sources(row.asset_id, row.revision)
        ],
    }
