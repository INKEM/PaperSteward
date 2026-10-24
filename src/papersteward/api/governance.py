"""P7-S5 全局权重治理 API：影响预览、确认激活与变更历史。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import ScoringChange, utc_now
from papersteward.errors import InvalidRequestError
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.governance import (
    confirm_weight_change,
    count_changes,
    get_change,
    list_changes,
    preview_from_config,
    preview_from_overrides,
)

router = APIRouter(prefix="/config/scoring", tags=["config"])


class ScoringChangeRead(BaseModel):
    id: str
    status: str
    base_version: str
    config_version: str
    preview_day: date
    effective_on: date
    preview_hash: str
    proposed: dict[str, Any]
    preview: dict[str, Any]
    created_at: datetime
    confirmed_at: datetime | None
    confirmed_by: str | None
    note: str | None


class ScoringChangeListResponse(BaseModel):
    items: list[ScoringChangeRead]
    total: int
    limit: int
    offset: int


class ScoringPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    version: str | None = None
    score_date: date | None = None
    effective_on: date | None = None
    limit: int = Field(default=20, ge=1, le=200)


class ScoringConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_by: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class ScoringConfirmResponse(ScoringChangeRead):
    static_recomputed: int


def _read(change: ScoringChange) -> ScoringChangeRead:
    return ScoringChangeRead(
        id=change.id,
        status=change.status.value,
        base_version=change.base_version,
        config_version=change.config_version,
        preview_day=change.preview_day,
        effective_on=change.effective_on,
        preview_hash=change.preview_hash,
        proposed=dict(change.proposed_json),
        preview=dict(change.preview_json),
        created_at=change.created_at,
        confirmed_at=change.confirmed_at,
        confirmed_by=change.confirmed_by,
        note=change.note,
    )


@router.get("/changes", response_model=ScoringChangeListResponse, summary="权重变更历史")
def changes(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ScoringChangeListResponse:
    items = list_changes(session, limit=limit, offset=offset)
    return ScoringChangeListResponse(
        items=[_read(item) for item in items],
        total=count_changes(session),
        limit=limit,
        offset=offset,
    )


@router.get("/changes/{change_id}", response_model=ScoringChangeRead, summary="权重变更详情")
def detail(change_id: str, session: SessionDep) -> ScoringChangeRead:
    return _read(get_change(session, change_id))


@router.post(
    "/preview",
    response_model=ScoringChangeRead,
    status_code=status.HTTP_201_CREATED,
    summary="预览全局权重影响",
)
def preview(body: ScoringPreviewRequest, session: SessionDep) -> ScoringChangeRead:
    if (body.overrides is None) == (body.config is None):
        raise InvalidRequestError("overrides 与 config 必须二选一")
    with domain_errors(), session.begin():
        timestamp = utc_now()
        if body.score_date is not None:
            day = body.score_date
        else:
            _, binding = bound_context(
                session,
                timestamp,
                action="config.preview",
                enforce_schedule=False,
            )
            day = runtime_local_day(session, timestamp, binding)
        effective = body.effective_on or day + timedelta(days=1)
        if body.config is not None:
            try:
                proposed = ScoringConfig.model_validate(body.config)
            except ValidationError as exc:
                raise InvalidRequestError("config 不合法") from exc
            change = preview_from_config(
                session,
                proposed,
                preview_day=day,
                effective_on=effective,
                limit=body.limit,
            )
        else:
            assert body.overrides is not None
            change = preview_from_overrides(
                session,
                body.overrides,
                version=body.version,
                preview_day=day,
                effective_on=effective,
                limit=body.limit,
            )
        return _read(change)


@router.post(
    "/changes/{change_id}/confirm",
    response_model=ScoringConfirmResponse,
    summary="确认并激活全局权重变更",
)
def confirm(
    change_id: str, body: ScoringConfirmRequest, session: SessionDep
) -> ScoringConfirmResponse:
    with domain_errors(), session.begin():
        change, recomputed = confirm_weight_change(
            session,
            change_id,
            confirmed_by=body.confirmed_by,
            note=body.note,
        )
        read = _read(change)
        return ScoringConfirmResponse(**read.model_dump(), static_recomputed=recomputed)


__all__ = ["router"]
