"""材料摄取与 Agent 草案生成：上传材料 → LLM 抽取 → 渲染为可校验草案。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.config import Settings
from papersteward.core.entities import Event, IntakeMaterial, ResearchProfile, Taxonomy, utc_now
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import OnboardingStage
from papersteward.errors import InvalidRequestError, NotFoundError
from papersteward.llm import LLMGateway, build_llm_provider
from papersteward.llm.runtime import resolve_llm_config
from papersteward.onboarding.profile import (
    latest_profile_draft,
    profile_review,
    save_draft,
)
from papersteward.onboarding.render import (
    PROFILE_ID,
    TAXONOMY_ID,
    next_draft_version,
    render_profile,
    render_taxonomy,
    version_key,
)
from papersteward.onboarding.service import enter_draft
from papersteward.onboarding.taxonomy import (
    latest_taxonomy_draft,
    parse_taxonomy_draft,
    save_taxonomy_draft,
    taxonomy_review,
)
from papersteward.schemas.models import ProfileExtractionOutput
from papersteward.storage.models import EventRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import (
    EventRepository,
    IntakeMaterialRepository,
    ProfileRepository,
    TaxonomyRepository,
)

MAX_MATERIALS = 20
# 页面表单整体上限 512 KiB；单份材料允许到该上限，合并给模型时再截断。
MAX_MATERIAL_CHARS = 512 * 1024
MAX_TOTAL_CHARS = 120_000
MAX_PREVIOUS_CHARS = 40_000
MAX_FEEDBACK_CHARS = 2_000


def _require_not_active(session: Session) -> None:
    if get_onboarding(session).stage is OnboardingStage.ACTIVE:
        raise InvariantViolation(
            "onboarding.stage", "初始化已完成；如需重做研究方向，请恢复旧版本或使用新的数据目录"
        )


def _require_draft(session: Session) -> None:
    if get_onboarding(session).stage is not OnboardingStage.DRAFT:
        raise InvariantViolation("onboarding.stage", "请先生成研究草案，再提交修改意见")


def _ordered(documents: list[Any]) -> list[Any]:
    return sorted(documents, key=lambda document: version_key(document.version))


def latest_draft_pair(
    session: Session,
) -> tuple[ResearchProfile | None, ResearchProfile | None, Taxonomy | None, Taxonomy | None]:
    """返回 (最新档案, 上一版档案, 最新分类法, 上一版分类法)，供审阅与 diff 使用。"""
    profiles = _ordered(
        [
            document
            for document in ProfileRepository(session).list(PROFILE_ID)
            if document.status is DocumentStatus.DRAFT
        ]
    )
    taxonomies = _ordered(
        [
            document
            for document in TaxonomyRepository(session).list(TAXONOMY_ID)
            if document.status is DocumentStatus.DRAFT
        ]
    )
    profile = profiles[-1] if profiles else None
    profile_base = profiles[-2] if len(profiles) > 1 else None
    taxonomy = taxonomies[-1] if taxonomies else None
    taxonomy_base = taxonomies[-2] if len(taxonomies) > 1 else None
    return profile, profile_base, taxonomy, taxonomy_base


def add_material(
    session: Session,
    *,
    name: str,
    content: str,
    kind: str = "text",
    occurred_at: datetime | None = None,
) -> IntakeMaterial:
    """保存一份材料；相同内容幂等返回已有记录。"""
    _require_not_active(session)
    repository = IntakeMaterialRepository(session)
    text = content.strip()
    if not text:
        raise DomainValidationError("intake.content", "材料内容不能为空")
    if len(text) > MAX_MATERIAL_CHARS:
        raise DomainValidationError(
            "intake.content", f"单份材料不能超过 {MAX_MATERIAL_CHARS // 1024} KiB，请拆分后上传"
        )
    if len(repository.list()) >= MAX_MATERIALS:
        raise DomainValidationError(
            "intake.materials", f"最多保留 {MAX_MATERIALS} 份材料，请先删除不需要的"
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = repository.find_by_sha256(digest)
    if existing is not None:
        return existing
    material = IntakeMaterial(
        id=f"mat-{uuid4().hex[:20]}",
        name=name.strip() or "未命名材料",
        kind=kind if kind in {"text", "file"} else "text",
        content=text,
        sha256=digest,
        created_at=occurred_at or utc_now(),
    )
    repository.add(material)
    EventRepository(session).append(
        Event(
            id=f"evt-intake-add-{material.id}",
            event_type=EventType.INTAKE_MATERIAL_ADDED,
            created_at=material.created_at,
            payload_json={"material_id": material.id, "name": material.name, "kind": material.kind},
        )
    )
    return material


def remove_material(
    session: Session, material_id: str, *, occurred_at: datetime | None = None
) -> None:
    _require_not_active(session)
    if not IntakeMaterialRepository(session).delete(material_id):
        raise NotFoundError("材料不存在", details={"id": material_id})
    EventRepository(session).append(
        Event(
            id=f"evt-intake-del-{material_id}-{uuid4().hex[:8]}",
            event_type=EventType.INTAKE_MATERIAL_REMOVED,
            created_at=occurred_at or utc_now(),
            payload_json={"material_id": material_id},
        )
    )


def list_materials(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": material.id,
            "name": material.name,
            "kind": material.kind,
            "chars": len(material.content),
            "created_at": material.created_at.isoformat(),
        }
        for material in IntakeMaterialRepository(session).list()
    ]


def add_feedback(
    session: Session, comment: str, *, occurred_at: datetime | None = None
) -> dict[str, Any]:
    """记录一条自然语言修改意见；返回事件负载。"""
    _require_draft(session)
    text = comment.strip()
    if not text:
        raise DomainValidationError("intake.feedback", "修改意见不能为空")
    if len(text) > MAX_FEEDBACK_CHARS:
        raise DomainValidationError("intake.feedback", "单条意见不能超过 2000 字符")
    timestamp = occurred_at or utc_now()
    profile = latest_profile_draft(session)
    taxonomy = latest_taxonomy_draft(session)
    payload = {
        "comment": text,
        "profile_version": profile.version if profile else None,
        "taxonomy_version": taxonomy.version if taxonomy else None,
    }
    EventRepository(session).append(
        Event(
            id=f"evt-intake-fb-{uuid4().hex[:20]}",
            event_type=EventType.INTAKE_FEEDBACK_RECORDED,
            created_at=timestamp,
            payload_json=payload,
        )
    )
    return payload


def recent_feedback(session: Session, *, limit: int = 5) -> list[str]:
    """最近的修改意见，从旧到新。"""
    rows = session.scalars(
        select(EventRow)
        .where(EventRow.event_type == EventType.INTAKE_FEEDBACK_RECORDED.value)
        .order_by(EventRow.created_at.desc(), EventRow.id.desc())
        .limit(limit)
    ).all()
    return [str(row.payload_json.get("comment", "")) for row in reversed(rows)]


def materials_text(session: Session) -> str:
    """合并全部材料；超出总长上限时截断并在合并文本末尾注明。"""
    parts: list[str] = []
    total = 0
    truncated = False
    for material in IntakeMaterialRepository(session).list():
        header = f"【材料：{material.name}】"
        body = material.content
        remaining = MAX_TOTAL_CHARS - total - len(header)
        if remaining <= 0:
            truncated = True
            continue
        if len(body) > remaining:
            body = body[:remaining]
            truncated = True
        parts.append(f"{header}\n{body}")
        total += len(header) + len(body)
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if truncated:
        combined += (
            "\n\n（材料总长超过上限，后续内容已截断；如需全部纳入，请删除或拆分材料后重试。）"
        )
    return combined


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（上一版内容过长，已截断）"


def _previous_drafts(session: Session) -> str:
    profile = latest_profile_draft(session)
    taxonomy = latest_taxonomy_draft(session)
    sections = []
    if profile is not None:
        sections.append(f"上一版研究方向档案（v{profile.version}）：\n{profile.content_md}")
    if taxonomy is not None:
        sections.append(f"上一版分类法（v{taxonomy.version}）：\n{taxonomy.content_md}")
    if not sections:
        return "（无，这是第一版）"
    return _truncate("\n\n".join(sections), MAX_PREVIOUS_CHARS)


def run_extraction(session: Session, settings: Settings) -> dict[str, Any]:
    """执行一次 Agent 抽取：材料 + 意见 → 新的 profile/taxonomy 草案。"""
    _require_draft(session)
    config = resolve_llm_config(session, settings)
    if problem := config.readiness_problem():
        raise InvalidRequestError(problem)
    material_text = materials_text(session)
    if not material_text.strip():
        raise InvalidRequestError("请先上传至少一份描述研究方向的材料")
    feedback = recent_feedback(session)
    previous = _previous_drafts(session)

    provider = build_llm_provider(config)
    try:
        gateway = LLMGateway(provider, max_attempts=config.max_attempts, timeout_s=config.timeout_s)
        result = gateway.complete_json(
            "profile_extraction",
            {
                "materials": material_text,
                "feedback": "\n".join(f"- {item}" for item in feedback) or "（无）",
                "previous": previous,
            },
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    extraction = cast(ProfileExtractionOutput, result.data)
    timestamp = utc_now()
    latest_profile, _base_profile, latest_taxonomy, _base_taxonomy = latest_draft_pair(session)
    existing_versions = [
        document.version for document in ProfileRepository(session).list(PROFILE_ID)
    ]
    version = next_draft_version(existing_versions)
    profile_md = render_profile(
        extraction, version=version, change_note=feedback[-1] if feedback else "依据材料生成"
    )
    profile_saved = save_draft(
        session,
        profile_md,
        PROFILE_ID,
        base_version=latest_profile.version if latest_profile else None,
    )
    taxonomy_versions = [
        document.version for document in TaxonomyRepository(session).list(TAXONOMY_ID)
    ]
    taxonomy_version = next_draft_version(taxonomy_versions)
    taxonomy_md = render_taxonomy(extraction, version=taxonomy_version)
    parse_taxonomy_draft(taxonomy_md, TAXONOMY_ID)  # 渲染结果必须可解析
    taxonomy_saved = save_taxonomy_draft(
        session,
        taxonomy_md,
        TAXONOMY_ID,
        base_version=latest_taxonomy.version if latest_taxonomy else None,
    )
    EventRepository(session).append(
        Event(
            id=f"evt-intake-gen-{uuid4().hex[:20]}",
            event_type=EventType.INTAKE_DRAFT_GENERATED,
            created_at=timestamp,
            payload_json={
                "profile_id": profile_saved["id"],
                "profile_version": profile_saved["version"],
                "taxonomy_id": taxonomy_saved["id"],
                "taxonomy_version": taxonomy_saved["version"],
                "model": result.provenance.model,
                "prompt_version": result.prompt_version,
                "repair_used": result.repair_used,
                "feedback_count": len(feedback),
            },
        )
    )
    return {
        "profile": profile_saved,
        "taxonomy": taxonomy_saved,
        "usage": result.usage.as_json(),
    }


def ensure_draft_stage(session: Session, *, occurred_at: datetime | None = None) -> None:
    """SETUP → DRAFT；幂等。生成草案前调用。"""
    enter_draft(
        session,
        event_id=f"evt-onboarding-{uuid4().hex}",
        occurred_at=occurred_at or utc_now(),
    )


def draft_summary(session: Session) -> dict[str, Any]:
    """最新草案与可确认状态；向导第三步的数据源。"""
    profile, profile_base, taxonomy, taxonomy_base = latest_draft_pair(session)
    materials = list_materials(session)
    review = None if profile is None else profile_review(profile, profile_base)
    taxonomy_draft = None if taxonomy is None else taxonomy_review(taxonomy, taxonomy_base)
    ready = bool(
        profile is not None and review is not None and review["ready"] and taxonomy is not None
    )
    return {
        "materials": materials,
        "feedback": recent_feedback(session, limit=10),
        "profile": review,
        "taxonomy": taxonomy_draft,
        "ready_to_confirm": ready,
        "legacy_drafts": legacy_drafts(session),
    }


def legacy_drafts(session: Session) -> list[dict[str, Any]]:
    """旧向导遗留的档案/分类法草稿（排除新流程固定 ID 与已导入材料）。"""
    found: list[dict[str, Any]] = []
    imported_hashes = {material.sha256 for material in IntakeMaterialRepository(session).list()}
    profiles = _ordered(
        [
            document
            for document in ProfileRepository(session).list()
            if document.id != PROFILE_ID
            and hashlib.sha256(document.content_md.encode()).hexdigest() not in imported_hashes
        ]
    )
    taxonomies = _ordered(
        [
            document
            for document in TaxonomyRepository(session).list()
            if document.id != TAXONOMY_ID
            and hashlib.sha256(document.content_md.encode()).hexdigest() not in imported_hashes
        ]
    )
    if profiles:
        latest = profiles[-1]
        found.append(
            {
                "kind": "profile",
                "id": latest.id,
                "version": latest.version,
                "status": latest.status.value,
            }
        )
    if taxonomies:
        latest = taxonomies[-1]
        found.append(
            {
                "kind": "taxonomy",
                "id": latest.id,
                "version": latest.version,
                "status": latest.status.value,
            }
        )
    return found


def import_legacy_drafts(
    session: Session, *, occurred_at: datetime | None = None
) -> list[IntakeMaterial]:
    """把旧向导最近一版档案/分类法导入为可编辑材料。"""
    if not legacy_drafts(session):
        raise InvalidRequestError("没有可导入的旧向导草稿")
    imported_hashes = {material.sha256 for material in IntakeMaterialRepository(session).list()}
    imported: list[IntakeMaterial] = []
    profiles = _ordered(
        [
            document
            for document in ProfileRepository(session).list()
            if document.id != PROFILE_ID
            and hashlib.sha256(document.content_md.encode()).hexdigest() not in imported_hashes
        ]
    )
    taxonomies = _ordered(
        [
            document
            for document in TaxonomyRepository(session).list()
            if document.id != TAXONOMY_ID
            and hashlib.sha256(document.content_md.encode()).hexdigest() not in imported_hashes
        ]
    )
    if profiles:
        profile = profiles[-1]
        imported.append(
            add_material(
                session,
                name=f"旧向导草稿：研究方向档案 v{profile.version}",
                content=profile.content_md,
                kind="text",
                occurred_at=occurred_at,
            )
        )
    if taxonomies:
        taxonomy = taxonomies[-1]
        imported.append(
            add_material(
                session,
                name=f"旧向导草稿：分类法 v{taxonomy.version}",
                content=taxonomy.content_md,
                kind="text",
                occurred_at=occurred_at,
            )
        )
    return imported


__all__ = [
    "MAX_MATERIALS",
    "MAX_MATERIAL_CHARS",
    "MAX_TOTAL_CHARS",
    "add_feedback",
    "add_material",
    "draft_summary",
    "ensure_draft_stage",
    "import_legacy_drafts",
    "latest_draft_pair",
    "legacy_drafts",
    "list_materials",
    "materials_text",
    "recent_feedback",
    "remove_material",
    "run_extraction",
]
