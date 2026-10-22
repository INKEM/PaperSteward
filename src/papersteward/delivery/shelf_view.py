"""P7-S3 特殊桶只读视图：档位、理由、证据与 kind 专属解释。

读取不写库；不返回 P/rank/V。三个特殊桶按设计顺序展示，空桶保持空列表，
不生成占位文献。未评估的已归属文献仍展示（``tier=null`` + needs_assessment）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Bucket,
    NominationEvidence,
    Paper,
    SpecialNominationRecord,
    SpecialTierSnapshot,
)
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    PaperStatus,
    ScoreTier,
    SpecialBucketKind,
    SpecialNominationStatus,
    TagRole,
)
from papersteward.errors import InvalidRequestError, NotFoundError
from papersteward.scoring.special import DEFAULT_SPECIAL_QUOTAS
from papersteward.storage.repositories import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    SpecialNominationRepository,
    SpecialTierRepository,
)

SHELF_DISPLAY_ORDER: Final = (
    SpecialBucketKind.REVIEW,
    SpecialBucketKind.DISTANT,
    SpecialBucketKind.CLASSIC,
)
_SHELF_NAMES: Final[Mapping[SpecialBucketKind, str]] = {
    SpecialBucketKind.REVIEW: "跨层综述",
    SpecialBucketKind.DISTANT: "远邻领域",
    SpecialBucketKind.CLASSIC: "经典/奠基",
}
ACTIVE_SHELF_STATUSES: Final = frozenset(
    {PaperStatus.SHELVED, PaperStatus.PUSHED, PaperStatus.READING, PaperStatus.DONE}
)
_TIER_ORDER: Final[Mapping[ScoreTier, int]] = {
    ScoreTier.HIGH: 0,
    ScoreTier.MEDIUM: 1,
    ScoreTier.LOW: 2,
}
ShelfTierFilter = ScoreTier | Literal["unrated"]


@dataclass(frozen=True, slots=True)
class ShelfSummary:
    """一个特殊桶的配额与档位分布；不包含跨桶排名。"""

    kind: SpecialBucketKind
    name: str
    bucket_id: str | None
    quota: int
    active: bool
    total: int
    high: int
    medium: int
    low: int
    unrated: int

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "bucket_id": self.bucket_id,
            "quota": self.quota,
            "active": self.active,
            "total": self.total,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "unrated": self.unrated,
        }


@dataclass(frozen=True, slots=True)
class ShelfItem:
    """只读特殊桶条目；``value``/P/rank 不属于展示契约。"""

    paper: Paper
    bucket: Bucket
    kind: SpecialBucketKind
    tier: SpecialTierSnapshot | None
    nomination: SpecialNominationRecord | None
    primary_tag: str | None
    secondary_tags: tuple[str, ...]

    @property
    def needs_assessment(self) -> bool:
        return self.tier is None

    @property
    def evidence(self) -> tuple[NominationEvidence, ...]:
        if self.tier is not None:
            return self.tier.evidence
        if self.nomination is not None:
            return self.nomination.evidence
        return ()

    def as_json(self) -> dict[str, Any]:
        explanation = list(self.tier.explanation) if self.tier is not None else []
        nomination = self.nomination
        return {
            "paper_id": self.paper.id,
            "title": self.paper.title,
            "authors": list(self.paper.authors),
            "venue": self.paper.venue,
            "published_at": (
                self.paper.published_at.isoformat() if self.paper.published_at is not None else None
            ),
            "url": self.paper.url,
            "status": self.paper.status.value,
            "kind": self.kind.value,
            "bucket_id": self.bucket.id,
            "tier": self.tier.tier.value if self.tier is not None else None,
            "needs_assessment": self.needs_assessment,
            "excluded_from_priority": True,
            "primary_tag": self.primary_tag,
            "secondary_tags": list(self.secondary_tags),
            "reason": nomination.reason if nomination is not None else "",
            "evidence": [{"quote": item.quote, "valid": item.valid} for item in self.evidence],
            "tier_explanation": explanation,
            "review_layers": list(nomination.review_layers) if nomination is not None else [],
            "classic_basis": list(nomination.classic_basis) if nomination is not None else [],
            "bridge_memo": (
                dict(nomination.bridge_memo)
                if nomination is not None and nomination.bridge_memo is not None
                else None
            ),
            "assessed_by": self.tier.assigned_by.value if self.tier is not None else None,
            "assessed_at": (self.tier.created_at.isoformat() if self.tier is not None else None),
            "tier_note": self.tier.note if self.tier is not None else None,
            "nomination_id": (
                self.tier.nomination_id
                if self.tier is not None and self.tier.nomination_id is not None
                else (nomination.id if nomination is not None else None)
            ),
        }


@dataclass(frozen=True, slots=True)
class ShelfPage:
    items: tuple[ShelfItem, ...]
    total: int
    limit: int
    offset: int
    summary: ShelfSummary

    def as_json(self) -> dict[str, Any]:
        return {
            "items": [item.as_json() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "shelf": self.summary.as_json(),
        }


def shelf_summaries(session: Session) -> tuple[ShelfSummary, ...]:
    """三个特殊桶的只读概览；桶尚未创建时返回空桶摘要，不写库。"""
    buckets = {
        bucket.special_kind: bucket
        for bucket in BucketRepository(session).list()
        if bucket.bucket_type is BucketType.SPECIAL and bucket.special_kind is not None
    }
    result: list[ShelfSummary] = []
    for kind in SHELF_DISPLAY_ORDER:
        bucket = buckets.get(kind)
        items = _items_for_bucket(session, kind, bucket)
        result.append(_summary(kind, bucket, items))
    return tuple(result)


def list_shelf(
    session: Session,
    kind: SpecialBucketKind,
    *,
    tier: ShelfTierFilter | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ShelfPage:
    """读取一个特殊桶；tier 过滤在高/中/低/未评估内进行。"""
    if not isinstance(kind, SpecialBucketKind):
        raise NotFoundError("特殊桶不存在")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise InvalidRequestError("limit 必须在 1–200 之间")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRequestError("offset 不能为负")
    bucket = _bucket_or_none(session, kind)
    items = _items_for_bucket(session, kind, bucket)
    summary = _summary(kind, bucket, items)
    filtered = [item for item in items if _matches_tier(item, tier)]
    return ShelfPage(
        items=tuple(filtered[offset : offset + limit]),
        total=len(filtered),
        limit=limit,
        offset=offset,
        summary=summary,
    )


def shelf_item(session: Session, kind: SpecialBucketKind, paper_id: str) -> ShelfItem:
    """读取某篇文献在某特殊桶的详情；不在该桶/非当前仓库状态返回 404。"""
    if not isinstance(kind, SpecialBucketKind):
        raise NotFoundError("特殊桶不存在")
    bucket = _bucket_or_none(session, kind)
    if bucket is None:
        raise NotFoundError("文献不在该特殊桶")
    paper = PaperRepository(session).get(paper_id)
    if paper is None or paper.status not in ACTIVE_SHELF_STATUSES:
        raise NotFoundError("文献不在该特殊桶")
    filings = [
        item
        for item in PaperBucketRepository(session).list_for_paper(paper_id)
        if item.bucket_id == bucket.id and item.role is BucketRole.FILING
    ]
    if not filings:
        raise NotFoundError("文献不在该特殊桶")
    return _item_for_paper(session, paper, bucket, kind)


def shelf_item_for_paper(session: Session, paper_id: str) -> ShelfItem | None:
    """按文献查找特殊桶归属；用于详情页避免把特殊桶当 P 解释。"""
    paper = PaperRepository(session).get(paper_id)
    if paper is None or paper.status not in ACTIVE_SHELF_STATUSES:
        return None
    filings = [
        item
        for item in PaperBucketRepository(session).list_for_paper(paper_id)
        if item.role is BucketRole.FILING
    ]
    if len(filings) != 1:
        return None
    bucket = BucketRepository(session).get(filings[0].bucket_id)
    if bucket is None or bucket.bucket_type is not BucketType.SPECIAL:
        return None
    kind = bucket.special_kind
    if kind is None:
        return None
    return _item_for_paper(session, paper, bucket, kind)


def parse_tier_filter(value: str | None) -> ShelfTierFilter | None:
    if value is None or not value.strip():
        return None
    text = value.strip().casefold()
    if text == "unrated":
        return "unrated"
    try:
        return ScoreTier(text)
    except ValueError as exc:
        raise InvalidRequestError("tier 必须是 high/medium/low/unrated") from exc


def parse_kind(value: str) -> SpecialBucketKind:
    try:
        return SpecialBucketKind(value)
    except ValueError as exc:
        raise NotFoundError("特殊桶不存在") from exc


def _items_for_bucket(
    session: Session, kind: SpecialBucketKind, bucket: Bucket | None
) -> list[ShelfItem]:
    if bucket is None:
        return []
    items: list[ShelfItem] = []
    for filing in PaperBucketRepository(session).list_filings(bucket.id):
        paper = PaperRepository(session).get(filing.paper_id)
        if paper is None or paper.status not in ACTIVE_SHELF_STATUSES:
            continue
        items.append(_item_for_paper(session, paper, bucket, kind))
    items.sort(
        key=lambda item: (
            _TIER_ORDER[item.tier.tier] if item.tier is not None else 3,
            item.paper.title.casefold(),
            item.paper.id,
        )
    )
    return items


def _item_for_paper(
    session: Session, paper: Paper, bucket: Bucket, kind: SpecialBucketKind
) -> ShelfItem:
    tags = PaperTagRepository(session).list_for_paper(paper.id)
    primary = next((tag.tag_code for tag in tags if tag.role is TagRole.PRIMARY), None)
    secondary = tuple(tag.tag_code for tag in tags if tag.role is TagRole.SECONDARY)
    nomination = next(
        (
            record
            for record in SpecialNominationRepository(session).list_for_paper(paper.id)
            if record.status is SpecialNominationStatus.ACCEPTED and record.kind is kind
        ),
        None,
    )
    tier = SpecialTierRepository(session).latest(paper.id, kind=kind)
    return ShelfItem(
        paper=paper,
        bucket=bucket,
        kind=kind,
        tier=tier,
        nomination=nomination,
        primary_tag=primary,
        secondary_tags=secondary,
    )


def _summary(
    kind: SpecialBucketKind, bucket: Bucket | None, items: list[ShelfItem]
) -> ShelfSummary:
    counts = Counter(item.tier.tier if item.tier is not None else None for item in items)
    return ShelfSummary(
        kind=kind,
        name=bucket.name if bucket is not None else _SHELF_NAMES[kind],
        bucket_id=bucket.id if bucket is not None else None,
        quota=(
            bucket.quota
            if bucket is not None and bucket.quota is not None
            else DEFAULT_SPECIAL_QUOTAS[kind]
        ),
        active=bucket.active if bucket is not None else False,
        total=len(items),
        high=counts.get(ScoreTier.HIGH, 0),
        medium=counts.get(ScoreTier.MEDIUM, 0),
        low=counts.get(ScoreTier.LOW, 0),
        unrated=counts.get(None, 0),
    )


def _bucket_or_none(session: Session, kind: SpecialBucketKind) -> Bucket | None:
    for bucket in BucketRepository(session).list():
        if bucket.bucket_type is BucketType.SPECIAL and bucket.special_kind is kind:
            return bucket
    return None


def _matches_tier(item: ShelfItem, tier: ShelfTierFilter | None) -> bool:
    if tier is None:
        return True
    if tier == "unrated":
        return item.tier is None
    return item.tier is not None and item.tier.tier is tier


__all__ = [
    "ACTIVE_SHELF_STATUSES",
    "SHELF_DISPLAY_ORDER",
    "ShelfItem",
    "ShelfPage",
    "ShelfSummary",
    "ShelfTierFilter",
    "list_shelf",
    "parse_kind",
    "parse_tier_filter",
    "shelf_item",
    "shelf_item_for_paper",
    "shelf_summaries",
]
