"""P5 日快照、只读队列和分数解释。"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from papersteward.api.deps import SessionDep
from papersteward.api.review import _review_errors
from papersteward.core.entities import utc_now
from papersteward.scoring.contracts import (
    Exclusion,
    QueueResponse,
    ScoreExplanation,
    SnapshotResponse,
)
from papersteward.scoring.queue import create_snapshots, explain_score, read_queue

router = APIRouter(tags=["priority"])


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score_date: date


@router.get("/queue", response_model=QueueResponse)
def queue(
    session: SessionDep,
    score_date: Annotated[date, Query(alias="date")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueueResponse:
    with _review_errors():
        return read_queue(session, score_date, now=utc_now(), limit=limit, offset=offset)


@router.post("/scores/snapshot", response_model=SnapshotResponse)
def snapshot(body: SnapshotRequest, session: SessionDep) -> SnapshotResponse:
    with _review_errors(), session.begin():
        return create_snapshots(session, body.score_date, now=utc_now())


@router.get("/scores/{paper_id}/explain", response_model=ScoreExplanation | Exclusion)
def explain(
    paper_id: str,
    session: SessionDep,
    score_date: Annotated[date | None, Query(alias="date")] = None,
) -> ScoreExplanation | Exclusion:
    with _review_errors():
        return explain_score(session, paper_id, now=utc_now(), day=score_date)
