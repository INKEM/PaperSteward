"""初始化状态与档案协商 API；G1 由可信业务校验器验证后显式确认。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from papersteward.api.deps import SessionDep
from papersteward.core.entities import utc_now
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, OnboardingStage
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.onboarding.profile import (
    MAX_PROFILE_BYTES,
    confirm_g1,
    load_profile,
    profile_review,
    save_draft,
)
from papersteward.onboarding.service import start
from papersteward.onboarding.taxonomy import (
    SeedLabel,
    confirm_g2,
    save_taxonomy_draft,
    taxonomy_review,
)
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


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=64)
    content_md: str = Field(min_length=1, max_length=MAX_PROFILE_BYTES)
    base_version: str | None = Field(default=None, min_length=1, max_length=32)


class ProfileReview(BaseModel):
    id: str
    version: str
    status: str
    content_md: str
    content_hash: str
    missing_fields: list[str]
    questions: list[str]
    g1_ready: bool
    diff: str | None
    markdown_diff: str | None


class ConfirmProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    confirmed_by: str = Field(min_length=1, max_length=128)
    change_summary: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@contextmanager
def _onboarding_errors() -> Iterator[None]:
    try:
        yield
    except IntegrityError as exc:
        raise ConflictError("文档版本已存在或状态已变化，请重新审阅") from exc
    except DomainError as exc:
        details: dict[str, Any] = {"field": exc.field}
        line = getattr(exc, "line", None)
        if line is not None:
            details["line"] = line
        error = ConflictError if isinstance(exc, InvariantViolation) else InvalidRequestError
        raise error(exc.message, details=details) from exc


@router.post("/profile/drafts", response_model=ProfileReview, status_code=201)
def create_profile_draft(body: DraftRequest, session: SessionDep) -> dict[str, Any]:
    with _onboarding_errors(), session.begin():
        return save_draft(session, body.content_md, body.profile_id, base_version=body.base_version)


@router.get("/profile/{profile_id}/{version}", response_model=ProfileReview)
def review_profile(
    profile_id: str, version: str, session: SessionDep, base_version: str | None = None
) -> dict[str, Any]:
    with _onboarding_errors():
        base = None if base_version is None else load_profile(session, profile_id, base_version)
        return profile_review(load_profile(session, profile_id, version), base)


@router.post("/profile/{profile_id}/{version}/confirm", response_model=OnboardingRead)
def confirm_profile_g1(
    profile_id: str, version: str, body: ConfirmProfileRequest, session: SessionDep
) -> OnboardingRead:
    with _onboarding_errors(), session.begin():
        state = confirm_g1(
            session,
            GateApproval(body.confirmed_by, profile_id, version, body.change_summary),
            content_hash=body.content_hash,
            expected_revision=body.expected_revision,
            event_id=f"evt-onboarding-{uuid4().hex}",
            occurred_at=utc_now(),
        )
    return OnboardingRead(stage=state.stage, revision=state.revision, next_gate=state.next_gate)


class TaxonomyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    taxonomy_id: str = Field(min_length=1, max_length=64)
    content_md: str = Field(min_length=1, max_length=MAX_PROFILE_BYTES)
    base_version: str | None = Field(default=None, min_length=1, max_length=32)
    seeds: list[SeedLabel] = Field(max_length=200)
    sample_kind: Literal["user", "synthetic"]


class TaxonomyReview(BaseModel):
    id: str
    version: str
    status: str
    content_md: str
    content_hash: str
    review_hash: str
    report: dict[str, Any]
    diff: str | None
    markdown_diff: str | None


class ConfirmTaxonomyRequest(ConfirmProfileRequest):
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/taxonomy/drafts", response_model=TaxonomyReview, status_code=201)
def create_taxonomy_draft(body: TaxonomyDraftRequest, session: SessionDep) -> dict[str, Any]:
    with _onboarding_errors(), session.begin():
        return save_taxonomy_draft(
            session,
            body.content_md,
            body.taxonomy_id,
            body.seeds,
            sample_kind=body.sample_kind,
            base_version=body.base_version,
            occurred_at=utc_now(),
        )


@router.get("/taxonomy/{taxonomy_id}/{version}", response_model=TaxonomyReview)
def review_taxonomy(taxonomy_id: str, version: str, session: SessionDep) -> dict[str, Any]:
    with _onboarding_errors():
        return taxonomy_review(session, taxonomy_id, version)


@router.post("/taxonomy/{taxonomy_id}/{version}/confirm", response_model=OnboardingRead)
def confirm_taxonomy_g2(
    taxonomy_id: str, version: str, body: ConfirmTaxonomyRequest, session: SessionDep
) -> OnboardingRead:
    with _onboarding_errors(), session.begin():
        state = confirm_g2(
            session,
            GateApproval(body.confirmed_by, taxonomy_id, version, body.change_summary),
            content_hash=body.content_hash,
            review_hash=body.review_hash,
            expected_revision=body.expected_revision,
            event_id=f"evt-onboarding-{uuid4().hex}",
            occurred_at=utc_now(),
        )
    return OnboardingRead(stage=state.stage, revision=state.revision, next_gate=state.next_gate)
