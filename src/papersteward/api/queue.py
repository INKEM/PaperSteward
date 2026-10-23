"""P5 日快照、只读队列和分数解释。"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.errors import InvalidRequestError
from papersteward.scoring.contracts import (
    BacklogFilters,
    BacklogResponse,
    Exclusion,
    QueueResponse,
    ScoreExplanation,
    SnapshotResponse,
)
from papersteward.scoring.queue import create_snapshots, explain_score, read_backlog, read_queue

router = APIRouter(tags=["priority"])


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score_date: date


@router.get("/queue", response_model=QueueResponse | BacklogResponse)
def queue(
    session: SessionDep,
    score_date: Annotated[date | None, Query(alias="date")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueueResponse | BacklogResponse:
    with domain_errors():
        if score_date is None:
            return read_backlog(session, now=utc_now(), limit=limit, offset=offset)
        return read_queue(session, score_date, now=utc_now(), limit=limit, offset=offset)


@router.post("/scores/snapshot", response_model=SnapshotResponse)
def snapshot(body: SnapshotRequest, session: SessionDep) -> SnapshotResponse:
    with domain_errors(), session.begin():
        return create_snapshots(session, body.score_date, now=utc_now())


@router.get("/scores/{paper_id}/explain", response_model=ScoreExplanation | Exclusion)
def explain(
    paper_id: str,
    session: SessionDep,
    score_date: Annotated[date | None, Query(alias="date")] = None,
) -> ScoreExplanation | Exclusion:
    with domain_errors():
        return explain_score(session, paper_id, now=utc_now(), day=score_date)


@router.get("/backlog", response_model=BacklogResponse)
def backlog(
    session: SessionDep,
    tag: str | None = None,
    min_priority: Annotated[float, Query(ge=0, le=100)] = 0,
    max_priority: Annotated[float, Query(ge=0, le=100)] = 100,
    read_status: Literal["unread", "read", "all"] = "unread",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BacklogResponse:
    if min_priority > max_priority:
        raise InvalidRequestError("最低分不能高于最高分")
    with domain_errors():
        return read_backlog(
            session,
            now=utc_now(),
            limit=limit,
            offset=offset,
            filters=BacklogFilters(
                tag=tag,
                min_priority=min_priority,
                max_priority=max_priority,
                read_status=read_status,
            ),
        )
