"""初始化 API：状态查询、材料摄取、草案生成与最终确认。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from papersteward.api.deps import SessionDep
from papersteward.api.workspace_runtime import resolve_workspace
from papersteward.config import get_settings
from papersteward.core.entities import utc_now as utc_now
from papersteward.core.errors import DomainError, InvariantViolation
from papersteward.core.onboarding import OnboardingStage
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.jobs.ui_flow import FlowRequest
from papersteward.onboarding.intake import (
    MAX_MATERIAL_CHARS,
    add_feedback,
    add_material,
    draft_summary,
    import_legacy_drafts,
    list_materials,
    remove_material,
)
from papersteward.onboarding.setup import confirm_setup
from papersteward.storage.onboarding import get_onboarding

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

_RUN_ID_PATTERN = r"^ext-[A-Za-z0-9][A-Za-z0-9._-]{0,59}$"


class OnboardingRead(BaseModel):
    stage: OnboardingStage
    revision: int


@router.get("", response_model=OnboardingRead)
def status(session: SessionDep) -> OnboardingRead:
    state = get_onboarding(session)
    return OnboardingRead(stage=state.stage, revision=state.revision)


class MaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="未命名材料", min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=MAX_MATERIAL_CHARS)
    kind: Literal["text", "file"] = "text"


class MaterialView(BaseModel):
    id: str
    name: str
    kind: str
    chars: int
    created_at: str


@contextmanager
def _onboarding_errors() -> Iterator[None]:
    try:
        yield
    except IntegrityError as exc:
        raise ConflictError("记录已存在或状态已变化，请刷新后重试") from exc
    except DomainError as exc:
        details: dict[str, Any] = {"field": exc.field}
        line = getattr(exc, "line", None)
        if line is not None:
            details["line"] = line
        error = ConflictError if isinstance(exc, InvariantViolation) else InvalidRequestError
        raise error(exc.message, details=details) from exc


@router.get("/intake/materials", response_model=list[MaterialView])
def read_materials(session: SessionDep) -> list[MaterialView]:
    return [MaterialView(**material) for material in list_materials(session)]


@router.post("/intake/materials", response_model=MaterialView, status_code=201)
def create_material(body: MaterialRequest, session: SessionDep) -> MaterialView:
    with _onboarding_errors(), session.begin():
        material = add_material(
            session, name=body.name, content=body.content, kind=body.kind, occurred_at=utc_now()
        )
    return MaterialView(
        id=material.id,
        name=material.name,
        kind=material.kind,
        chars=len(material.content),
        created_at=material.created_at.isoformat(),
    )


@router.delete("/intake/materials/{material_id}")
def delete_material(material_id: str, session: SessionDep) -> dict[str, bool]:
    with _onboarding_errors(), session.begin():
        remove_material(session, material_id, occurred_at=utc_now())
    return {"deleted": True}


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(min_length=1, max_length=2000)


@router.get("/drafts")
def read_drafts(session: SessionDep) -> dict[str, Any]:
    return draft_summary(session)


@router.post("/intake/import-legacy", response_model=list[MaterialView], status_code=201)
def import_legacy(session: SessionDep) -> list[MaterialView]:
    """旧向导走到一半时，把遗留档案/分类法导入为材料继续新流程。"""
    with _onboarding_errors(), session.begin():
        imported = import_legacy_drafts(session, occurred_at=utc_now())
    return [
        MaterialView(
            id=material.id,
            name=material.name,
            kind=material.kind,
            chars=len(material.content),
            created_at=material.created_at.isoformat(),
        )
        for material in imported
    ]


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)


class ExtractAccepted(BaseModel):
    run_id: str


def _enqueue_extraction(request: Request, run_id: str | None) -> str:
    identity = run_id or f"ext-{uuid4().hex[:16]}"
    with _onboarding_errors():
        resolve_workspace(request).ui_flows.submit(FlowRequest(run_id=identity, kind="extract"))
    return identity


@router.post("/intake/generate", response_model=ExtractAccepted, status_code=202)
def generate_draft(body: ExtractRequest, request: Request, session: SessionDep) -> ExtractAccepted:
    """把材料交给后台 Agent 生成草案；立即返回 Run ID，进度见 /ui/runs/{id}。"""
    stage = get_onboarding(session).stage
    if stage is OnboardingStage.ACTIVE:
        raise ConflictError("初始化已完成；如需重做研究方向，请使用新的数据目录")
    if stage is not OnboardingStage.DRAFT and not list_materials(session):
        raise InvalidRequestError("请先上传至少一份描述研究方向的材料")
    return ExtractAccepted(run_id=_enqueue_extraction(request, body.run_id))


class FeedbackAccepted(ExtractAccepted):
    feedback: dict[str, Any]


@router.post("/intake/feedback", response_model=FeedbackAccepted, status_code=202)
def submit_feedback(
    body: FeedbackRequest, request: Request, session: SessionDep
) -> FeedbackAccepted:
    """记录自然语言意见并立即生成下一版草案。"""
    with _onboarding_errors(), session.begin():
        payload = add_feedback(session, body.comment, occurred_at=utc_now())
    identity = _enqueue_extraction(request, None)
    return FeedbackAccepted(run_id=identity, feedback=payload)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    confirmed_by: str = Field(min_length=1, max_length=128)
    change_summary: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_version: str = Field(min_length=1, max_length=32)
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_id: str = Field(min_length=1, max_length=64)
    taxonomy_version: str = Field(min_length=1, max_length=32)
    taxonomy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/confirm", response_model=OnboardingRead)
def confirm(body: ConfirmRequest, session: SessionDep) -> OnboardingRead:
    with _onboarding_errors(), session.begin():
        state = confirm_setup(
            session,
            confirmed_by=body.confirmed_by,
            change_summary=body.change_summary,
            expected_revision=body.expected_revision,
            profile_id=body.profile_id,
            profile_version=body.profile_version,
            profile_hash=body.profile_hash,
            taxonomy_id=body.taxonomy_id,
            taxonomy_version=body.taxonomy_version,
            taxonomy_hash=body.taxonomy_hash,
            timezone=get_settings().timezone,
            event_id=f"evt-onboarding-{uuid4().hex}",
            occurred_at=utc_now(),
        )
    return OnboardingRead(stage=state.stage, revision=state.revision)


__all__ = ["router"]
