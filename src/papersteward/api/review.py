"""P4 审核队列 API：列出待审项、批准或人工改标签。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from papersteward.api.deps import SessionDep
from papersteward.core.entities import Paper, ReviewItem
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.pipeline.review import (
    approve_review,
    list_open_reviews,
    relabel_review,
)
from papersteward.storage.repositories import PaperRepository

router = APIRouter(prefix="/review", tags=["review"])


class ReviewItemRead(BaseModel):
    paper_id: str
    title: str
    paper_status: PaperStatus
    reasons: list[str]
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ReviewListResponse(BaseModel):
    items: list[ReviewItemRead]
    total: int
    limit: int
    offset: int


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed_by: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class ReviewRelabel(ReviewAction):
    primary_tag: str = Field(min_length=1, max_length=32)
    secondary_tags: list[str] = Field(default_factory=list, max_length=5)


@contextmanager
def _review_errors() -> Iterator[None]:
    try:
        yield
    except InvariantViolation as exc:
        raise ConflictError(exc.message, details={"field": exc.field}) from exc
    except DomainError as exc:
        raise InvalidRequestError(exc.message, details={"field": exc.field}) from exc


def _read(item: ReviewItem, paper: Paper) -> ReviewItemRead:
    return ReviewItemRead(
        paper_id=item.paper_id,
        title=paper.title,
        paper_status=paper.status,
        reasons=list(item.reasons),
        payload=dict(item.payload_json),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", response_model=ReviewListResponse, summary="列出待审核文献")
def list_review(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewListResponse:
    pairs, total = list_open_reviews(session, limit=limit, offset=offset)
    return ReviewListResponse(
        items=[_read(item, paper) for item, paper in pairs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{paper_id}/approve", response_model=ReviewItemRead, summary="批准审核项")
def approve(paper_id: str, body: ReviewAction, session: SessionDep) -> ReviewItemRead:
    with _review_errors(), session.begin():
        item = approve_review(session, paper_id, reviewed_by=body.reviewed_by, note=body.note)
        paper = PaperRepository(session).get(paper_id)
        if paper is None:
            raise NotFoundError("文献不存在")
        return _read(item, paper)


@router.post("/{paper_id}/relabel", response_model=ReviewItemRead, summary="人工改标签")
def relabel(paper_id: str, body: ReviewRelabel, session: SessionDep) -> ReviewItemRead:
    with _review_errors(), session.begin():
        item = relabel_review(
            session,
            paper_id,
            primary_tag=body.primary_tag,
            secondary_tags=body.secondary_tags,
            reviewed_by=body.reviewed_by,
            note=body.note,
        )
        paper = PaperRepository(session).get(paper_id)
        if paper is None:
            raise NotFoundError("文献不存在")
        return _read(item, paper)


__all__ = ["router"]
