"""P7-S4 自定义排序视图 API：预设、预览、保存/加载。

预览只读当前合格队列并重排，不写全局配置/评分快照；保存只写 view_config。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from papersteward.api.deps import SessionDep
from papersteward.api.review import _review_errors
from papersteward.core.entities import ViewConfig, utc_now
from papersteward.scoring.views import (
    ResolvedView,
    ViewPreset,
    ViewPreview,
    ViewRankedItem,
    delete_view_config,
    formula,
    get_view_config,
    list_presets,
    list_view_configs,
    preview_view,
    save_view,
)

router = APIRouter(prefix="/views", tags=["views"])


class ViewPresetRead(BaseModel):
    name: str
    label: str
    description: str
    weights: dict[str, float]
    normalized_weights: dict[str, float]
    formula: str


class ViewDefinitionRead(BaseModel):
    name: str
    label: str
    description: str
    preset: str | None
    is_preset: bool
    weights: dict[str, float]
    formula: str


class ViewConfigRead(BaseModel):
    id: str
    name: str
    weights: dict[str, float]
    preset: str | None
    description: str | None
    formula: str
    created_at: datetime


class ViewComponentRead(BaseModel):
    score: float
    weight: float
    contribution: float


class ViewPreviewItemRead(BaseModel):
    paper_id: str
    title: str
    primary_tag: str
    summary_100: str
    published_on: date | None
    rank: int
    default_rank: int
    delta_rank: int
    view_score: float
    default_priority: float
    default_rank_score: float
    components: dict[str, ViewComponentRead]


class ViewPreviewRead(BaseModel):
    date: date
    view: ViewDefinitionRead
    formula: str
    candidate_total: int
    changed: int
    items: list[ViewPreviewItemRead]
    total: int
    limit: int
    offset: int


class ViewPresetListResponse(BaseModel):
    items: list[ViewPresetRead]
    total: int
    limit: int
    offset: int


class ViewListResponse(BaseModel):
    items: list[ViewConfigRead]
    total: int
    limit: int
    offset: int


class ViewSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    weights: dict[str, float] | None = None
    preset: str | None = None
    description: str | None = Field(default=None, max_length=2000)


class ViewPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score_date: date | None = None
    preset: str | None = None
    weights: dict[str, float] | None = None
    view_id: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def _read_preset(item: ViewPreset) -> ViewPresetRead:
    return ViewPresetRead.model_validate(item.as_json())


def _read_config(item: ViewConfig) -> ViewConfigRead:
    return ViewConfigRead(
        id=item.id,
        name=item.name,
        weights=dict(item.weights),
        preset=item.preset,
        description=item.description,
        formula=formula(item.weights),
        created_at=item.created_at,
    )


def _read_view(item: ResolvedView) -> ViewDefinitionRead:
    return ViewDefinitionRead.model_validate(item.as_json())


def _read_item(item: ViewRankedItem) -> ViewPreviewItemRead:
    return ViewPreviewItemRead.model_validate(item.as_json())


def _read_preview(item: ViewPreview) -> ViewPreviewRead:
    return ViewPreviewRead(
        date=item.score_date,
        view=_read_view(item.view),
        formula=formula(item.view.weights),
        candidate_total=item.candidate_total,
        changed=item.changed,
        items=[_read_item(entry) for entry in item.items],
        total=item.total,
        limit=item.limit,
        offset=item.offset,
    )


@router.get("/presets", response_model=ViewPresetListResponse, summary="自定义视图预设")
def presets() -> ViewPresetListResponse:
    items = [_read_preset(item) for item in list_presets()]
    return ViewPresetListResponse(items=items, total=len(items), limit=len(items), offset=0)


@router.get("", response_model=ViewListResponse, summary="列出已保存视图")
def list_views(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ViewListResponse:
    items = list_view_configs(session)
    return ViewListResponse(
        items=[_read_config(item) for item in items[offset : offset + limit]],
        total=len(items),
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ViewConfigRead,
    status_code=status.HTTP_201_CREATED,
    summary="保存自定义视图",
)
def save(body: ViewSaveRequest, session: SessionDep) -> ViewConfigRead:
    with session.begin():
        view = save_view(
            session,
            body.name,
            preset=body.preset,
            weights=body.weights,
            description=body.description,
        )
        return _read_config(view)


@router.get("/{view_id}", response_model=ViewConfigRead, summary="读取已保存视图")
def detail(view_id: str, session: SessionDep) -> ViewConfigRead:
    return _read_config(get_view_config(session, view_id))


@router.delete(
    "/{view_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除已保存视图",
)
def delete(view_id: str, session: SessionDep) -> Response:
    with session.begin():
        delete_view_config(session, view_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/preview", response_model=ViewPreviewRead, summary="预览自定义排序")
def preview(body: ViewPreviewRequest, session: SessionDep) -> ViewPreviewRead:
    with _review_errors():
        result = preview_view(
            session,
            body.score_date or utc_now().date(),
            preset=body.preset,
            weights=body.weights,
            view_id=body.view_id,
            limit=body.limit,
            offset=body.offset,
        )
    return _read_preview(result)


__all__ = ["router"]
