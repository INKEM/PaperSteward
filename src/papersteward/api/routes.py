"""P0-S3 路由：健康检查与 papers stub 列表。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from papersteward import __version__
from papersteward.api.deps import SessionDep
from papersteward.api.schemas import HealthResponse, PaperListResponse, PaperRead
from papersteward.storage import PaperRepository, check_database

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    tags=["system"],
    summary="健康检查",
)
def health(request: Request, response: Response) -> HealthResponse:
    database_ok = check_database(request.app.state.engine)
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    settings = request.app.state.settings
    return HealthResponse(
        status="ok" if database_ok else "degraded",
        version=__version__,
        environment=settings.app_env.value,
        database="ok" if database_ok else "unavailable",
    )


@router.get(
    "/papers",
    response_model=PaperListResponse,
    tags=["papers"],
    summary="列出文献",
)
def list_papers(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaperListResponse:
    repository = PaperRepository(session)
    papers = repository.list(limit=limit, offset=offset)
    return PaperListResponse(
        items=[PaperRead.model_validate(paper) for paper in papers],
        total=repository.count(),
        limit=limit,
        offset=offset,
    )


__all__ = ["router"]
