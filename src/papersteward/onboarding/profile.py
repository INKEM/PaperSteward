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
from papersteward.documents.diff import DiffEntry
from papersteward.documents.frontmatter import split_front_matter
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.render import version_key
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository

MAX_PROFILE_BYTES = 128 * 1024


def describe_profile_changes(entries: list[DiffEntry]) -> list[str]:
    """面向阅读的变更摘要（UXR-12）；逐行原始 diff 仍可在审阅页展开查看。"""
    changes: list[str] = []

    def added_removed(prefix: str, label: str) -> None:
        added = sorted(
            e.path.split(".")[1]
            for e in entries
            if e.change == "add" and e.path.startswith(prefix) and e.path.count(".") == 1
        )
        removed = sorted(
            e.path.split(".")[1]
            for e in entries
            if e.change == "remove" and e.path.startswith(prefix) and e.path.count(".") == 1
        )
        if added:
            changes.append(f"新增{label}：" + "、".join(added))
        if removed:
            changes.append(f"移除{label}：" + "、".join(removed))

    added_removed("subdirections.", "子方向")
    if any(e.path == "positioning.summary" for e in entries):
        changes.append("研究定位描述有调整")
    if any(e.path.startswith("positioning.") and e.path != "positioning.summary" for e in entries):
        changes.append("定位信息有调整")
    if any(e.path.startswith("core_questions.") for e in entries):
        changes.append("核心问题有调整")
    shares = sorted(
        {
            e.path.split(".")[1]
            for e in entries
            if e.path.endswith(".target_share") and e.change == "change"
        }
    )
    if shares:
        changes.append("子方向占比有调整（" + "、".join(shares) + "）")
    if any(
        e.change == "change"
        and e.path.startswith("subdirections.")
        and not e.path.endswith(".target_share")
        for e in entries
    ):
        changes.append("子方向内容有调整")
    if any(e.path.startswith("reading.") for e in entries):
        changes.append("阅读节奏有调整")
    if not changes:
        changes.append("研究档案有微调")
    return changes


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
    entries = None if base is None else diff_profiles(base, profile)
    return {
        "id": profile.id,
        "version": profile.version,
        "status": profile.status.value,
        "content_md": profile.content_md,
        "content_hash": content_hash(profile.content_md),
        "missing_fields": missing,
        "ready": not missing,
        "diff": None if entries is None else render_diff(entries),
        # UXR-12：面向阅读的变更摘要，默认展示；原始逐行 diff 折叠在审阅页。
        "change_summary": None if entries is None else describe_profile_changes(entries),
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
