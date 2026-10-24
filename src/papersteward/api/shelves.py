"""P7-S3 特殊桶只读 API：概览、分页列表与详情。

响应不包含 V、P、rank_score；三个特殊桶各自展示档位、理由、证据与 kind 专属解释。
读取不写库，也不自动补评估。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from papersteward.api.deps import SessionDep
from papersteward.core.enums import PaperStatus, ScoreTier, SpecialBucketKind
from papersteward.reading.shelf_view import (
    ShelfItem,
    ShelfSummary,
    list_shelf,
    parse_kind,
    parse_tier_filter,
    shelf_item,
    shelf_summaries,
)

router = APIRouter(prefix="/shelves", tags=["shelves"])
TierQuery = Literal["high", "medium", "low", "unrated"]


class ShelfEvidenceRead(BaseModel):
    quote: str
    valid: bool


class ShelfItemRead(BaseModel):
    paper_id: str
    title: str
    authors: list[str]
    venue: str | None
    published_at: datetime | None
    url: str | None
    status: PaperStatus
    kind: SpecialBucketKind
    bucket_id: str
    tier: ScoreTier | None
    needs_assessment: bool
    excluded_from_priority: bool = True
    primary_tag: str | None
    secondary_tags: list[str]
    reason: str
    evidence: list[ShelfEvidenceRead]
    tier_explanation: list[str]
    review_layers: list[str]
    classic_basis: list[str]
    bridge_memo: dict[str, Any] | None
    assessed_by: str | None
    assessed_at: datetime | None
    tier_note: str | None
    tier_reviewed_by: str | None
    nomination_id: str | None


class ShelfSummaryRead(BaseModel):
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


class ShelfOverviewResponse(BaseModel):
    items: list[ShelfSummaryRead]
    total: int
    limit: int
    offset: int


class ShelfItemsResponse(BaseModel):
    items: list[ShelfItemRead]
    total: int
    limit: int
    offset: int
    shelf: ShelfSummaryRead


def _read_item(item: ShelfItem) -> ShelfItemRead:
    return ShelfItemRead.model_validate(item.as_json())


def _read_summary(summary: ShelfSummary) -> ShelfSummaryRead:
    return ShelfSummaryRead.model_validate(summary.as_json())


@router.get("", response_model=ShelfOverviewResponse, summary="特殊桶概览")
def overview(session: SessionDep) -> ShelfOverviewResponse:
    summaries = [_read_summary(item) for item in shelf_summaries(session)]
    return ShelfOverviewResponse(
        items=summaries,
        total=len(summaries),
        limit=len(summaries),
        offset=0,
    )


@router.get("/{kind}", response_model=ShelfItemsResponse, summary="列出特殊桶内容")
def shelf(
    kind: str,
    session: SessionDep,
    tier: Annotated[TierQuery | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ShelfItemsResponse:
    page = list_shelf(
        session,
        parse_kind(kind),
        tier=parse_tier_filter(tier),
        limit=limit,
        offset=offset,
    )
    return ShelfItemsResponse(
        items=[_read_item(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        shelf=_read_summary(page.summary),
    )


@router.get("/{kind}/{paper_id}", response_model=ShelfItemRead, summary="特殊桶条目详情")
def detail(kind: str, paper_id: str, session: SessionDep) -> ShelfItemRead:
    return _read_item(shelf_item(session, parse_kind(kind), paper_id))


__all__ = ["router"]
