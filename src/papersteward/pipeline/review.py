"""P4 审核队列：打开、列出、批准与人工改标签；事务由调用方控制。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Paper,
    PaperBucket,
    PaperTag,
    ReviewItem,
    Taxonomy,
    utc_now,
)
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    DocumentStatus,
    PaperStatus,
    ReviewResolution,
    TagLevel,
    TagRole,
)
from papersteward.errors import ConflictError, NotFoundError
from papersteward.storage.repositories import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ReviewRepository,
    TaxonomyRepository,
)


def open_review(
    session: Session,
    paper_id: str,
    reasons: Sequence[str],
    payload: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> ReviewItem:
    return ReviewRepository(session).open(paper_id, reasons, payload, now=now or utc_now())


def list_open_reviews(
    session: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[tuple[ReviewItem, Paper]], int]:
    items, total = ReviewRepository(session).list_open(limit=limit, offset=offset)
    papers = PaperRepository(session)
    result: list[tuple[ReviewItem, Paper]] = []
    for item in items:
        paper = papers.get(item.paper_id)
        if paper is not None:
            result.append((item, paper))
    return result, total


def _paper_or_404(session: Session, paper_id: str) -> Paper:
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    return paper


def _open_item_or_404(session: Session, paper_id: str) -> ReviewItem:
    item = ReviewRepository(session).get(paper_id)
    if item is None:
        raise NotFoundError("审核项不存在")
    return item


def _transition_after_review(session: Session, paper: Paper, *, now: datetime, action: str) -> None:
    if paper.status is PaperStatus.CLASSIFIED:
        return
    if paper.status is not PaperStatus.NEEDS_REVIEW:
        raise ConflictError("论文当前状态不可批准/改标签")
    PaperRepository(session).transition(
        paper.id,
        PaperStatus.CLASSIFIED,
        event_id=uuid4().hex,
        occurred_at=now,
        payload={"stage": "review", "action": action},
    )


def approve_review(
    session: Session,
    paper_id: str,
    *,
    reviewed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewItem:
    now = now or utc_now()
    item = _open_item_or_404(session, paper_id)
    if item.status.value != "open":
        raise ConflictError("审核项已处理")
    paper = _paper_or_404(session, paper_id)
    _transition_after_review(session, paper, now=now, action="approve")
    return ReviewRepository(session).resolve(
        paper_id,
        resolved_by=reviewed_by,
        resolution=ReviewResolution.APPROVED,
        note=note,
        now=now,
    )


def _review_taxonomy(session: Session, item: ReviewItem) -> Taxonomy:
    identifier = item.payload_json.get("taxonomy_id")
    version = item.payload_json.get("taxonomy_version")
    repository = TaxonomyRepository(session)
    taxonomy: Taxonomy | None = None
    if isinstance(identifier, str) and isinstance(version, str):
        taxonomy = repository.get(identifier, version)
    if taxonomy is None:
        taxonomy = repository.latest_confirmed()
    if taxonomy is None or taxonomy.status is not DocumentStatus.CONFIRMED:
        raise ConflictError("审核项缺少可用的已确认分类法")
    return taxonomy


def _relabel_tags(
    taxonomy: Taxonomy, paper_id: str, primary_tag: str, secondary_tags: Sequence[str]
) -> tuple[PaperTag, ...]:
    primary = next(
        (
            tag
            for tag in taxonomy.tags
            if tag.code == primary_tag and tag.level is TagLevel.PRIMARY and tag.active
        ),
        None,
    )
    if primary is None:
        raise ConflictError("主标签不在已确认分类法中或未激活")
    codes = [primary_tag]
    secondary: list[PaperTag] = []
    for code in secondary_tags:
        if code in codes:
            raise ConflictError("副标签不能重复或等于主标签")
        tag = next(
            (
                item
                for item in taxonomy.tags
                if item.code == code and item.level is TagLevel.SECONDARY and item.active
            ),
            None,
        )
        if tag is None:
            raise ConflictError(f"副标签 {code} 不在已确认分类法中")
        codes.append(code)
        secondary.append(
            PaperTag(
                paper_id=paper_id,
                tag_code=code,
                role=TagRole.SECONDARY,
                taxonomy_version=taxonomy.version,
                confidence=1.0,
                assigned_by=AssignedBy.USER,
            )
        )
    if len(secondary) > 5:
        raise ConflictError("副标签最多 5 个")
    return (
        PaperTag(
            paper_id=paper_id,
            tag_code=primary_tag,
            role=TagRole.PRIMARY,
            taxonomy_version=taxonomy.version,
            confidence=1.0,
            assigned_by=AssignedBy.USER,
        ),
        *secondary,
    )


def relabel_review(
    session: Session,
    paper_id: str,
    *,
    primary_tag: str,
    secondary_tags: Sequence[str] = (),
    reviewed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewItem:
    now = now or utc_now()
    item = _open_item_or_404(session, paper_id)
    if item.status.value != "open":
        raise ConflictError("审核项已处理")
    paper = _paper_or_404(session, paper_id)
    taxonomy = _review_taxonomy(session, item)
    tags = _relabel_tags(taxonomy, paper_id, primary_tag, secondary_tags)
    bucket = next(
        (
            candidate
            for candidate in BucketRepository(session).list(active_only=True)
            if candidate.bucket_type is BucketType.TOPIC and candidate.tag_code == primary_tag
        ),
        None,
    )
    if bucket is None:
        raise ConflictError("主标签没有可用的激活主题桶")
    PaperTagRepository(session).replace_all(paper_id, tags)
    PaperBucketRepository(session).replace_filing(
        paper_id,
        PaperBucket(
            paper_id=paper_id,
            bucket_id=bucket.id,
            role=BucketRole.FILING,
            assigned_by=AssignedBy.USER,
            reason="review relabel",
        ),
    )
    _transition_after_review(session, paper, now=now, action="relabel")
    return ReviewRepository(session).resolve(
        paper_id,
        resolved_by=reviewed_by,
        resolution=ReviewResolution.RELABELED,
        note=note,
        now=now,
    )


__all__ = [
    "approve_review",
    "list_open_reviews",
    "open_review",
    "relabel_review",
]
