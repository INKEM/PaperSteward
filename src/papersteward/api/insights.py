"""阅读回顾 JSON 接口；统计计算在服务层，路由不重复实现。"""

from typing import Any

from fastapi import APIRouter, status

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.reading.insights.contracts import ReadTimeInput, ReadTimeView
from papersteward.reading.insights.timeline import read_time_view, record_reading_time

router = APIRouter(prefix="/reading-insights", tags=["reading-insights"])


@router.get("/papers/{paper_id}/read-time", response_model=ReadTimeView, summary="查看阅读时间")
def get_read_time(paper_id: str, session: SessionDep) -> Any:
    with domain_errors():
        return read_time_view(session, paper_id)


@router.post(
    "/papers/{paper_id}/read-time",
    response_model=ReadTimeView,
    status_code=status.HTTP_201_CREATED,
    summary="补记或更正阅读时间",
)
def post_read_time(paper_id: str, body: ReadTimeInput, session: SessionDep) -> Any:
    with domain_errors(), session.begin():
        return record_reading_time(
            session,
            paper_id,
            body,
            now=utc_now(),
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
