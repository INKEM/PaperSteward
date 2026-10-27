"""手工资产（收获/问题）的创建、修订、状态与删除；只引用已读归档。

语义（阶段计划 §5.3/§6 R1-05、R2-00B）：
- 版本只追加；每次修订提交全量内容 + ``expected_revision`` 乐观并发，
  旧版本与当时来源原样保留。修订请求省略来源时保留当前来源集合原样
  （含归档哈希与反馈版本），显式给出列表才整体替换。
- 来源只信服务端：逐个校验归档快照存在并复制其内容哈希；
  客户端传来的任何来源内容或确认状态不采信。
- 幂等键绑定操作、目标与规范化请求哈希：同键同请求重放原结果，
  不同请求明确冲突；回执与业务写入同一事务，失败回滚后键不被消耗。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.insights.contracts import (
    TYPED_ASSET_KINDS,
    AssetCreateInput,
    AssetRevisionDetail,
    AssetRevisionInput,
    AssetRevisionSummary,
    AssetSourceInput,
    AssetSourceRef,
    AssetSourceStatus,
    AssetView,
    EvidenceGroupContent,
    MethodCardContent,
)
from papersteward.reading.insights.idempotency import (
    ASSET_CREATE,
    ASSET_REVISE,
    ASSET_STATUS,
    ReplayResult,
    asset_content_payload,
    complete_keyed_write,
    discard_keyed_write,
    open_keyed_write,
)
from papersteward.reading.insights.reports import delete_report, list_reports, read_report
from papersteward.storage.insights import (
    ReadingAssetRepository,
    ReadingAssetTypedRepository,
    ReadingDerivedCopyRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    PaperRow,
    RatingRow,
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
)

_KIND_DEFAULT_STATUS = {
    "harvest": "active",
    "problem": "open",
    "method": "active",
    "evidence": "active",
}
_KIND_ALLOWED_STATUS = {
    "harvest": {"active", "archived"},
    "problem": {"open", "resolved", "archived"},
    "method": {"active", "archived"},
    "evidence": {"active", "archived"},
}
_KIND_LABELS = {"harvest": "收获", "problem": "问题", "method": "方法卡", "evidence": "证据组"}


def create_asset(session: Session, spec: AssetCreateInput, *, now: datetime) -> AssetView:
    """创建资产；同键同请求重放原视图，不同请求明确冲突。"""
    repository = ReadingAssetRepository(session)
    # 来源校验先于回执：非法输入不消耗幂等键。
    resolved = _resolve_sources(session, spec.sources)
    outcome = open_keyed_write(
        session,
        operation=ASSET_CREATE,
        target_id="",
        key=spec.idempotency_key,
        payload=asset_content_payload(
            spec.kind,
            title=spec.title,
            body=spec.body,
            applicability=spec.applicability,
            sources=_request_sources(spec.sources),
            typed=_typed_payload(spec.typed),
        ),
        now=as_utc(now),
    )
    if isinstance(outcome, ReplayResult):
        if outcome.result is not None:
            return AssetView.model_validate(outcome.result)
        legacy = repository.get_by_idempotency(spec.idempotency_key)
        if legacy is None:
            raise InvariantViolation("asset.request_id", "无法定位该请求ID对应的资产，请更换后重试")
        return get_asset_view(session, legacy.id)
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
    _put_typed(session, asset_id, 1, spec.kind, spec.typed)
    view = get_asset_view(session, asset_id)
    complete_keyed_write(outcome, target_id=asset_id, result=view.model_dump(mode="json"))
    return view


def append_revision(
    session: Session, asset_id: str, spec: AssetRevisionInput, *, now: datetime
) -> AssetView:
    """追加全量内容版本；同键同请求重放原结果，不同请求明确冲突。"""
    return _append_revision(session, asset_id, spec, operation=ASSET_REVISE, now=now)


def change_status(
    session: Session,
    asset_id: str,
    status: str,
    *,
    now: datetime,
    expected_revision: int,
    idempotency_key: str,
) -> AssetView:
    """只改状态的便捷修订（问题解决/重开、归档）；内容与来源沿用当前版本。"""
    view = get_asset_view(session, asset_id)
    current = view.revision
    spec = AssetRevisionInput.model_validate(
        {
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "title": current.title,
            "body": current.body,
            "applicability": current.applicability,
            "status": status,
            "sources": None,
        }
    )
    return _append_revision(session, asset_id, spec, operation=ASSET_STATUS, now=now)


def _append_revision(
    session: Session,
    asset_id: str,
    spec: AssetRevisionInput,
    *,
    operation: str,
    now: datetime,
) -> AssetView:
    """修订核心：回执先于版本检查（重试重放），来源省略即保留当前集合。"""
    repository = ReadingAssetRepository(session)
    asset = repository.get_asset(asset_id)
    if asset is None:
        raise NotFoundError("资产不存在")
    current = repository.max_revision(asset_id)
    _check_status(asset.kind, spec.status)
    resolved = (
        _preserved_sources(repository, asset_id, current)
        if spec.sources is None
        else _resolve_sources(session, spec.sources)
    )
    typed_payload = _typed_for_revision(session, asset_id, current, spec.typed)
    outcome = open_keyed_write(
        session,
        operation=operation,
        target_id=asset_id,
        key=spec.idempotency_key,
        payload=asset_content_payload(
            None,
            title=spec.title,
            body=spec.body,
            applicability=spec.applicability,
            sources=[
                (paper_id, as_utc(rating_at).isoformat() if rating_at else None)
                for paper_id, _, rating_at in resolved
            ],
            typed=typed_payload,
        ),
        now=as_utc(now),
    )
    if isinstance(outcome, ReplayResult):
        if outcome.result is not None:
            return AssetView.model_validate(outcome.result)
        return get_asset_view(session, asset_id)
    if spec.expected_revision != current:
        # 版本冲突可修正后重试：显式移除回执，不依赖调用方回滚也不消耗键。
        discard_keyed_write(outcome)
        raise ConflictError(
            "资产已被其他页面更新，请基于最新版本重试",
            details={"current_revision": current},
        )
    try:
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
    except IntegrityError:
        # 真并发下复合主键兜底触发：先回滚解除会话失败状态，转为可恢复冲突。
        session.rollback()
        raise ConflictError(
            "资产正被其他请求同时修订，请基于最新版本重试",
            details={"current_revision": current},
        ) from None
    _put_typed(session, asset_id, current + 1, asset.kind, typed_payload)
    view = get_asset_view(session, asset_id)
    complete_keyed_write(outcome, result=view.model_dump(mode="json"))
    return view


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


def get_asset_revision_detail(
    session: Session, asset_id: str, revision_number: int
) -> AssetRevisionDetail:
    """逐版本读取：旧正文、适用说明与当时来源原样返回，引用状态明确标注。

    版本不存在时 404，不回退到最新内容。
    """
    repository = ReadingAssetRepository(session)
    asset = repository.get_asset(asset_id)
    if asset is None:
        raise NotFoundError("资产不存在")
    row = repository.revision(asset_id, revision_number)
    if row is None:
        raise NotFoundError("资产版本不存在")
    source_status: list[AssetSourceStatus] = []
    for source in repository.sources(asset_id, revision_number):
        paper_exists = session.get(PaperRow, source.paper_id) is not None
        rating = (
            session.get(RatingRow, {"paper_id": source.paper_id, "created_at": source.rating_at})
            if source.rating_at is not None
            else None
        )
        source_status.append(
            AssetSourceStatus(
                paper_id=source.paper_id,
                archive_hash=source.archive_hash,
                rating_at=as_utc(source.rating_at) if source.rating_at else None,
                paper_exists=paper_exists,
                rating_exists=rating is not None,
                rating_score=rating.score if rating is not None else None,
                rating_note=rating.note if rating is not None else None,
            )
        )
    return AssetRevisionDetail(
        asset_id=asset.id,
        kind=asset.kind,
        revision=_revision_view(repository, row),
        source_status=source_status,
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
    """删除范围预览：版本数、来源引用数、含私人副本的报告清单。

    私人成分包括资产自身的标题/正文/适用说明（随资产删除），以及已保存
    报告中的标题/正文副本——仅标题副本的报告同样列入 ``private_copies``
    （F06）；不含私人字段的引用（如脱敏副本）归入 ``referenced_reports``。
    """
    repository = ReadingAssetRepository(session)
    if repository.get_asset(asset_id) is None:
        raise NotFoundError("资产不存在")
    revisions = repository.revisions(asset_id)
    source_count = sum(len(repository.sources(asset_id, row.revision)) for row in revisions)
    copies, referenced = _private_copies_for_asset(session, asset_id)
    return {
        "revision_count": len(revisions),
        "source_count": source_count,
        "private_copies": copies,
        "referenced_reports": referenced,
    }


def delete_asset(
    session: Session, asset_id: str, *, also_report_ids: Sequence[str] = ()
) -> list[str]:
    """删除资产；勾选的含私人副本报告一并删除（清掉其中的标题/正文副本）。"""
    repository = ReadingAssetRepository(session)
    if repository.get_asset(asset_id) is None:
        raise NotFoundError("资产不存在")
    preview = delete_preview(session, asset_id)
    removable = {item["report_id"] for item in preview["private_copies"]}
    removed: list[str] = []
    for report_id in also_report_ids:
        if report_id not in removable:
            raise InvalidRequestError(
                "该报告不含此资产的私人副本，无需一并删除",
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


def _private_copies_for_asset(
    session: Session, asset_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """扫描报告冻结正文：含私人副本的清单与不含副本的普通引用。

    以实际内容为准（保存路径同时维护 reading_derived_copy 登记表供撤回复用）；
    清单按报告生成时间倒序，字段类别区分标题、正文与知识整理，仅标题副本必须入选。
    知识版报告的私人归纳登记在派生链中，即使事实正文没有该资产引用也要列出。
    """
    summaries = []
    offset = 0
    while True:
        listing = list_reports(session, limit=200, offset=offset)
        summaries.extend(listing["items"])
        offset += len(listing["items"])
        if offset >= listing["total"]:
            break
    copies: list[dict[str, Any]] = []
    referenced: list[str] = []
    for summary in summaries:
        try:
            detail = read_report(session, summary.report_id)
        except (InvariantViolation, NotFoundError):
            # 未知契约或损坏报告无法扫描；不因它阻断删除预览。
            continue
        refs = [ref for ref in detail.content.assets if ref.asset_id == asset_id]
        if not refs:
            continue
        fields: list[str] = []
        revision: int | None = None
        for ref in refs:
            if ref.revision is not None:
                revision = ref.revision
            if ref.title:
                fields.append("title")
            if ref.body is not None:
                fields.append("body")
        if fields:
            copies.append(
                {
                    "report_id": detail.report_id,
                    "fields": fields,
                    "source_revision": revision,
                    "generated_at": detail.generated_at,
                }
            )
        else:
            referenced.append(detail.report_id)
    known = {item["report_id"] for item in copies} | set(referenced)
    for lineage in ReadingDerivedCopyRepository(session).for_source("asset", asset_id):
        if lineage.derived_kind != "report" or lineage.field_kind != "synthesis":
            continue
        if lineage.derived_id in known:
            continue
        try:
            detail = read_report(session, lineage.derived_id)
        except (InvariantViolation, NotFoundError):
            continue
        known.add(detail.report_id)
        copies.append(
            {
                "report_id": detail.report_id,
                "fields": ["synthesis"],
                "source_revision": lineage.source_revision,
                "generated_at": detail.generated_at,
            }
        )
    return copies, referenced


def _request_sources(sources: Sequence[AssetSourceInput]) -> list[tuple[str, str | None]]:
    """把请求来源转成哈希输入；paper_id 去重保留首个，与 _resolve_sources 一致。"""
    seen: set[str] = set()
    pairs: list[tuple[str, str | None]] = []
    for source in sources:
        if source.paper_id in seen:
            continue
        seen.add(source.paper_id)
        pairs.append(
            (
                source.paper_id,
                as_utc(source.rating_at).isoformat() if source.rating_at else None,
            )
        )
    return pairs


def _preserved_sources(
    repository: ReadingAssetRepository, asset_id: str, revision: int
) -> list[tuple[str, str, datetime | None]]:
    """保留当前版本的来源原样：paper_id + archive_hash + rating_at 不重解析。"""
    rows = repository.sources(asset_id, revision)
    if not rows:
        raise DomainValidationError("asset.sources", "当前版本没有可保留的来源，请显式提供")
    return [
        (row.paper_id, row.archive_hash, as_utc(row.rating_at) if row.rating_at else None)
        for row in rows
    ]


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
    """当前版本视图；来源按 (asset_id, revision) 取当时集合，含类型化内容。"""
    typed_row = ReadingAssetTypedRepository(repository._session).get(  # noqa: SLF001 - 同包仓储协作
        row.asset_id, row.revision
    )
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
        "typed": dict(typed_row.content_json) if typed_row is not None else None,
    }


def _typed_payload(typed: MethodCardContent | EvidenceGroupContent | None) -> dict[str, Any] | None:
    """类型化内容的幂等载荷：规范化 dict；None 不进哈希。"""
    if typed is None:
        return None
    return typed.model_dump(mode="json")


def _put_typed(
    session: Session,
    asset_id: str,
    revision: int,
    kind: str,
    typed: MethodCardContent | EvidenceGroupContent | dict[str, Any] | None,
) -> None:
    """写入类型化内容行；非类型化 kind 或 None（保留原样）不写。

    ``typed`` 可为契约对象或已解析的规范化 dict（修订路径）。
    """
    if kind not in TYPED_ASSET_KINDS or typed is None:
        return
    content = typed.model_dump(mode="json") if isinstance(typed, BaseModel) else dict(typed)
    ReadingAssetTypedRepository(session).put(asset_id, revision, content)


def _typed_for_revision(
    session: Session,
    asset_id: str,
    revision: int,
    typed: MethodCardContent | EvidenceGroupContent | None,
) -> dict[str, Any] | None:
    """修订的 typed 解析：显式给出生效，省略（None）保留当前版本内容。"""
    if typed is not None:
        return typed.model_dump(mode="json")
    current = ReadingAssetTypedRepository(session).get(asset_id, revision)
    return dict(current.content_json) if current is not None else None
