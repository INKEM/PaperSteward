"""阅读操作API，所有写入复用同一事务服务。"""

from typing import Any

from fastapi import APIRouter

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.delivery.actions import apply_action
from papersteward.delivery.contracts import Action

router = APIRouter(tags=["reading"])


@router.post("/papers/{paper_id}/actions")
def action(paper_id: str, body: Action, session: SessionDep) -> dict[str, Any]:
    with domain_errors(), session.begin():
        return apply_action(session, paper_id, body, now=utc_now())
