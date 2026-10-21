"""S1 仅提供开始/状态；后续业务服务接入证据校验后再开放各门确认。"""

from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel

from papersteward.api.deps import SessionDep
from papersteward.core.entities import utc_now
from papersteward.core.onboarding import Gate, OnboardingStage
from papersteward.onboarding.service import start
from papersteward.storage.onboarding import get_onboarding

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingRead(BaseModel):
    stage: OnboardingStage
    revision: int
    next_gate: Gate | None


@router.get("", response_model=OnboardingRead)
def status(session: SessionDep) -> OnboardingRead:
    state = get_onboarding(session)
    return OnboardingRead(stage=state.stage, revision=state.revision, next_gate=state.next_gate)


@router.post("/start", response_model=OnboardingRead)
def start_onboarding(session: SessionDep) -> OnboardingRead:
    with session.begin():
        state = start(session, event_id=f"evt-onboarding-{uuid4().hex}", occurred_at=utc_now())
    return OnboardingRead(stage=state.stage, revision=state.revision, next_gate=state.next_gate)
