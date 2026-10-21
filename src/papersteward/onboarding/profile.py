"""P2-S2：档案草案审阅、追加版本与 G1 原子确认。"""

import hashlib
from datetime import datetime
from difflib import unified_diff
from typing import Any

import yaml
from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile
from papersteward.core.enums import DocumentStatus
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage
from papersteward.documents import confirm_profile, diff_profiles, parse_profile, render_diff
from papersteward.documents.frontmatter import DocumentError, split_front_matter
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.service import confirm_gate
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository

MAX_PROFILE_BYTES = 128 * 1024


def parse_draft(content_md: str, profile_id: str) -> ResearchProfile:
    """限长后解析事实源；禁止可展开的 YAML 引用，不改 P1 解析契约。"""
    if len(content_md.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise DomainValidationError("profile.content_md", "档案不能超过 128 KiB")
    front = split_front_matter(content_md, source="onboarding")
    try:
        if any(
            isinstance(token, yaml.AliasToken | yaml.AnchorToken)
            for token in yaml.scan(front.yaml_text)
        ):
            raise DomainValidationError("profile.content_md", "请展开 YAML 锚点/别名后重新提交")
    except yaml.YAMLError:
        pass  # 下方解析器提供统一的字段/行号错误。
    profile = parse_profile(content_md, source="onboarding", document_id=profile_id)
    embedded_id = profile.parsed_json.get("profile_id")
    if embedded_id is not None and embedded_id != profile.id:
        raise DomainValidationError("profile.profile_id", "正文 profile_id 必须与请求一致")
    if len(profile.version) > 32:
        raise DomainValidationError("profile.version", "版本号最多 32 个字符")
    return profile


def g1_missing_fields(profile: ResearchProfile) -> list[str]:
    """结构要求已由解析器检查，这里只补充初始化完整性条件。"""
    data = profile.parsed_json
    missing = []
    if not data.get("exclude"):
        missing.append("exclude")
    goals = data["reading"].get("reading_goals", [])
    if not goals or any(not goal.strip() for goal in goals):
        missing.append("reading.reading_goals")
    for direction in data["subdirections"]:
        keywords = direction.get("keywords", [])
        if not keywords or any(not word.strip() for word in keywords):
            missing.append(f"subdirections.{direction['id']}.keywords")
    return missing


def load_profile(session: Session, profile_id: str, version: str) -> ResearchProfile:
    profile = ProfileRepository(session).get(profile_id, version)
    if profile is None:
        raise NotFoundError("档案版本不存在", details={"id": profile_id, "version": version})
    return profile


def profile_review(profile: ResearchProfile, base: ResearchProfile | None = None) -> dict[str, Any]:
    missing = g1_missing_fields(parse_draft(profile.content_md, profile.id))
    questions = [f"请补充 {field}，使研究边界与阅读目标可执行。" for field in missing]
    questions.extend(
        [
            "哪些看似相关的论文实际不希望推送？请核对排除标准。",
            "各子方向的目标阅读占比是否符合当前主线与探索需求？",
            "阅读容量与精读/粗读比例是否可持续？",
        ]
    )
    return {
        "id": profile.id,
        "version": profile.version,
        "status": profile.status.value,
        "content_md": profile.content_md,
        "content_hash": hashlib.sha256(profile.content_md.encode("utf-8")).hexdigest(),
        "missing_fields": missing,
        "questions": questions[:5],
        "g1_ready": not missing,
        "diff": None if base is None else render_diff(diff_profiles(base, profile)),
        "markdown_diff": None
        if base is None
        else "\n".join(
            unified_diff(
                base.content_md.splitlines(),
                profile.content_md.splitlines(),
                fromfile=base.version,
                tofile=profile.version,
                lineterm="",
            )
        ),
    }


def save_draft(
    session: Session, content_md: str, profile_id: str, *, base_version: str | None = None
) -> dict[str, Any]:
    if get_onboarding(session).stage is not OnboardingStage.PROFILE:
        raise InvariantViolation("onboarding.stage", "请先开始初始化；仅档案协商阶段允许编辑")
    profile = parse_draft(content_md, profile_id)
    repository = ProfileRepository(session)
    if repository.get(profile.id, profile.version) is not None:
        raise ConflictError("档案版本已存在；编辑请提升 version")
    if base_version is None and repository.list(profile.id):
        raise ConflictError("编辑已有档案时必须指定 base_version")
    base = None if base_version is None else load_profile(session, profile.id, base_version)
    review = profile_review(profile, base)
    repository.add(profile)
    return review


def confirm_g1(
    session: Session,
    approval: GateApproval,
    *,
    content_hash: str,
    expected_revision: int,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    def validate(session: Session, state: Onboarding, gate: Gate, approval: GateApproval) -> None:
        profile = load_profile(session, approval.artifact_id, approval.artifact_version)
        if profile.status is DocumentStatus.ARCHIVED:
            raise InvariantViolation("profile.status", "已归档档案不能通过 G1")
        parsed = parse_draft(profile.content_md, profile.id)
        if parsed.version != profile.version or dict(parsed.parsed_json) != dict(
            profile.parsed_json
        ):
            raise InvariantViolation(
                "profile.parsed_json", "档案缓存与正文不一致，请重新导入新版本"
            )
        if profile_review(profile)["content_hash"] != content_hash:
            raise InvariantViolation("profile.content_hash", "档案内容已变化，请重新审阅后确认")
        missing = g1_missing_fields(parsed)
        if missing:
            raise DocumentError("请补全后确认 G1", source="onboarding", field=missing[0])
        if profile.status is DocumentStatus.DRAFT:
            confirm_profile(session, profile.id, profile.version, confirmed_at=occurred_at)

    return confirm_gate(
        session,
        Gate.G1,
        approval,
        expected_revision=expected_revision,
        validate_evidence=validate,
        event_id=event_id,
        occurred_at=occurred_at,
    )
