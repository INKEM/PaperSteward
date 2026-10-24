"""显式周度任务触发，无启动自动任务。"""

from typing import Any

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from pydantic import Field

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.delivery.contracts import InputModel
from papersteward.jobs.preference import refresh_weekly

router = APIRouter(prefix="/jobs", tags=["jobs"])


class WeeklyRequest(InputModel):
    run_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )


@router.post("/preference/weekly")
def weekly(body: WeeklyRequest, session: SessionDep) -> dict[str, Any]:
    with domain_errors(), session.begin():
        return dict(jsonable_encoder(refresh_weekly(session, now=utc_now(), run_id=body.run_id)))
