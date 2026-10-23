"""研究档案草案：解析、完整性检查、版本化保存与审阅视图。"""

import hashlib
from difflib import unified_diff
from typing import Any

import yaml
from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import OnboardingStage
from papersteward.documents import diff_profiles, parse_profile, render_diff
from papersteward.documents.frontmatter import split_front_matter
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.render import version_key
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository

MAX_PROFILE_BYTES = 128 * 1024


def parse_draft(content_md: str, profile_id: str) -> ResearchProfile:
    """限长后解析事实源；禁止可展开的 YAML 引用，不改解析契约。"""
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


def missing_fields(profile: ResearchProfile) -> list[str]:
    """结构要求已由解析器检查，这里只补充完整性条件。"""
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


# 兼容旧名称；新代码使用 missing_fields。
g1_missing_fields = missing_fields


def load_profile(session: Session, profile_id: str, version: str) -> ResearchProfile:
    profile = ProfileRepository(session).get(profile_id, version)
    if profile is None:
        raise NotFoundError("档案版本不存在", details={"id": profile_id, "version": version})
    return profile


def latest_profile_draft(session: Session, profile_id: str | None = None) -> ResearchProfile | None:
    """返回最新的草案版本（按数字版本号取最大）。"""
    documents = ProfileRepository(session).list(profile_id)
    drafts = [document for document in documents if document.status.value == "draft"]
    if not drafts:
        return None
    return max(drafts, key=lambda document: version_key(document.version))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def profile_review(profile: ResearchProfile, base: ResearchProfile | None = None) -> dict[str, Any]:
    missing = missing_fields(parse_draft(profile.content_md, profile.id))
    return {
        "id": profile.id,
        "version": profile.version,
        "status": profile.status.value,
        "content_md": profile.content_md,
        "content_hash": content_hash(profile.content_md),
        "missing_fields": missing,
        "ready": not missing,
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
    if get_onboarding(session).stage is not OnboardingStage.DRAFT:
        raise InvariantViolation("onboarding.stage", "请先在设置页配置模型并上传材料生成草案")
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


__all__ = [
    "MAX_PROFILE_BYTES",
    "content_hash",
    "g1_missing_fields",
    "latest_profile_draft",
    "load_profile",
    "missing_fields",
    "parse_draft",
    "profile_review",
    "save_draft",
]
