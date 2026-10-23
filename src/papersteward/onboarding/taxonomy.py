"""分类法草案：解析、版本化保存与审阅视图。"""

import hashlib
from difflib import unified_diff
from typing import Any

import yaml
from sqlalchemy.orm import Session

from papersteward.core.entities import Taxonomy
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import OnboardingStage
from papersteward.documents import diff_taxonomies, parse_taxonomy, render_diff
from papersteward.documents.frontmatter import load_yaml_mapping, split_front_matter
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.profile import MAX_PROFILE_BYTES
from papersteward.onboarding.render import version_key
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import TaxonomyRepository


def parse_taxonomy_draft(text: str, taxonomy_id: str) -> Taxonomy:
    if len(text.encode()) > MAX_PROFILE_BYTES:
        raise DomainValidationError("taxonomy.content_md", "分类法不能超过 128 KiB")
    front = split_front_matter(text, source="onboarding")
    try:
        if any(
            isinstance(token, yaml.AliasToken | yaml.AnchorToken)
            for token in yaml.scan(front.yaml_text)
        ):
            raise DomainValidationError("taxonomy.content_md", "请展开 YAML 锚点/别名后重新提交")
    except yaml.YAMLError:
        pass
    metadata = load_yaml_mapping(front, source="onboarding")
    taxonomy = parse_taxonomy(text, source="onboarding", document_id=taxonomy_id)
    if metadata.get("taxonomy_id", taxonomy.id) != taxonomy.id or len(taxonomy.version) > 32:
        raise DomainValidationError("taxonomy.identity", "taxonomy_id 必须匹配，版本号最多 32 字符")
    return taxonomy


def load_taxonomy(session: Session, taxonomy_id: str, version: str) -> Taxonomy:
    taxonomy = TaxonomyRepository(session).get(taxonomy_id, version)
    if taxonomy is None:
        raise NotFoundError("分类法版本不存在", details={"id": taxonomy_id, "version": version})
    return taxonomy


def latest_taxonomy_draft(session: Session, taxonomy_id: str | None = None) -> Taxonomy | None:
    documents = TaxonomyRepository(session).list(taxonomy_id)
    drafts = [document for document in documents if document.status.value == "draft"]
    if not drafts:
        return None
    return max(drafts, key=lambda document: version_key(document.version))


def taxonomy_review(taxonomy: Taxonomy, base: Taxonomy | None = None) -> dict[str, Any]:
    return {
        "id": taxonomy.id,
        "version": taxonomy.version,
        "status": taxonomy.status.value,
        "content_md": taxonomy.content_md,
        "content_hash": content_hash(taxonomy.content_md),
        "primary_tags": [
            {"code": tag.code, "name": tag.name}
            for tag in taxonomy.tags
            if tag.level.value == "primary"
        ],
        "diff": None if base is None else render_diff(diff_taxonomies(base, taxonomy)),
        "markdown_diff": None
        if base is None
        else "\n".join(
            unified_diff(
                base.content_md.splitlines(),
                taxonomy.content_md.splitlines(),
                fromfile=base.version,
                tofile=taxonomy.version,
                lineterm="",
            )
        ),
    }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def save_taxonomy_draft(
    session: Session, content_md: str, taxonomy_id: str, *, base_version: str | None = None
) -> dict[str, Any]:
    if get_onboarding(session).stage is not OnboardingStage.DRAFT:
        raise InvariantViolation("onboarding.stage", "请先在设置页配置模型并上传材料生成草案")
    taxonomy = parse_taxonomy_draft(content_md, taxonomy_id)
    repository = TaxonomyRepository(session)
    if repository.get(taxonomy.id, taxonomy.version) is not None:
        raise ConflictError("分类法版本已存在；编辑请提升 version")
    if base_version is None and repository.list(taxonomy.id):
        raise ConflictError("编辑已有分类法必须指定 base_version")
    base = None if base_version is None else load_taxonomy(session, taxonomy.id, base_version)
    review = taxonomy_review(taxonomy, base)
    repository.add(taxonomy)
    return review


__all__ = [
    "content_hash",
    "latest_taxonomy_draft",
    "load_taxonomy",
    "parse_taxonomy_draft",
    "save_taxonomy_draft",
    "taxonomy_review",
]
