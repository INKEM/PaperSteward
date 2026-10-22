"""P4 分类：主/副标签、置信度/边界、逐字证据与标签/桶持久化。

只接受已确认 taxonomy；``LLMGateway`` 负责一次 schema 修复。低置信度、接近的次优
标签、边界说明或证据不足进入 ``needs_review``，但不静默丢弃分类结果。不改变
PaperStatus，不处理特殊桶（P4-S6）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Bucket,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    Tag,
    Taxonomy,
)
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    DocumentStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import DomainError
from papersteward.core.invariants import (
    DEFAULT_CLASSIFICATION_MARGIN,
    DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD,
)
from papersteward.llm import LLMGateway
from papersteward.schemas import ClassificationOutput
from papersteward.storage.repositories import PaperBucketRepository, PaperTagRepository

CLASSIFICATION_PROMPT_VERSION = "classification/v2"
MIN_EVIDENCE_CHARS = 4
MAX_FREE_TAG_CHARS = 64


class ClassificationError(DomainError):
    """分类输入/分类法/持久化冲突；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    quote: str
    valid: bool

    def __post_init__(self) -> None:
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise ClassificationError("classification.evidence.quote", "不能为空")
        if not isinstance(self.valid, bool):
            raise ClassificationError("classification.evidence.valid", "必须是布尔值")
        object.__setattr__(self, "quote", self.quote.strip())


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    paper_id: str
    taxonomy_id: str
    taxonomy_version: str
    classification: ClassificationOutput
    primary_tag: PaperTag
    secondary_tags: tuple[PaperTag, ...]
    bucket: PaperBucket
    evidence: tuple[ClassificationEvidence, ...]
    needs_review: bool
    reasons: tuple[str, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        for name in ("paper_id", "taxonomy_id", "taxonomy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ClassificationError(f"classification.{name}", "不能为空")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.classification, ClassificationOutput):
            raise ClassificationError("classification.output", "必须是 ClassificationOutput")
        if not isinstance(self.primary_tag, PaperTag):
            raise ClassificationError("classification.primary_tag", "必须是 PaperTag")
        if self.primary_tag.paper_id != self.paper_id:
            raise ClassificationError("classification.primary_tag.paper_id", "与 paper_id 不一致")
        if self.primary_tag.role is not TagRole.PRIMARY:
            raise ClassificationError("classification.primary_tag.role", "必须是主标签")
        if self.primary_tag.tag_code != self.classification.primary_tag:
            raise ClassificationError("classification.primary_tag.tag_code", "与模型输出不一致")
        if self.primary_tag.taxonomy_version != self.taxonomy_version:
            raise ClassificationError(
                "classification.primary_tag.taxonomy_version", "与 taxonomy_version 不一致"
            )
        if not isinstance(self.secondary_tags, tuple) or any(
            not isinstance(tag, PaperTag) for tag in self.secondary_tags
        ):
            raise ClassificationError("classification.secondary_tags", "必须是 PaperTag 元组")
        for tag in self.secondary_tags:
            if tag.paper_id != self.paper_id or tag.role is not TagRole.SECONDARY:
                raise ClassificationError("classification.secondary_tags", "必须是本文副标签")
            if tag.taxonomy_version != self.taxonomy_version:
                raise ClassificationError(
                    "classification.secondary_tags.taxonomy_version", "与 taxonomy_version 不一致"
                )
        if not isinstance(self.bucket, PaperBucket):
            raise ClassificationError("classification.bucket", "必须是 PaperBucket")
        if self.bucket.paper_id != self.paper_id or self.bucket.role is not BucketRole.FILING:
            raise ClassificationError("classification.bucket", "必须是本文的归属桶")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, ClassificationEvidence) for item in self.evidence
        ):
            raise ClassificationError(
                "classification.evidence", "必须是 ClassificationEvidence 元组"
            )
        if not isinstance(self.needs_review, bool):
            raise ClassificationError("classification.needs_review", "必须是布尔值")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reasons
        ):
            raise ClassificationError("classification.reasons", "必须是非空字符串元组")
        if self.needs_review != bool(self.reasons):
            raise ClassificationError("classification.needs_review", "必须与 reasons 一致")
        if not isinstance(self.provenance, Provenance):
            raise ClassificationError("classification.provenance", "必须是 Provenance")

    @property
    def tags(self) -> tuple[PaperTag, ...]:
        return (self.primary_tag, *self.secondary_tags)

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.taxonomy_version,
            "primary_tag": _tag_json(self.primary_tag),
            "secondary_tags": [_tag_json(tag) for tag in self.secondary_tags],
            "bucket_id": self.bucket.bucket_id,
            "runner_up_tag": self.classification.runner_up_tag,
            "runner_up_confidence": self.classification.runner_up_confidence,
            "boundary_note": self.classification.boundary_note,
            "evidence": [{"quote": item.quote, "valid": item.valid} for item in self.evidence],
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
            "provenance": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "generated_at": self.provenance.generated_at.isoformat(),
                "input_hash": self.provenance.input_hash,
            },
        }


def _tag_json(tag: PaperTag) -> dict[str, Any]:
    return {
        "tag_code": tag.tag_code,
        "role": tag.role.value,
        "taxonomy_version": tag.taxonomy_version,
        "confidence": tag.confidence,
        "assigned_by": tag.assigned_by.value,
    }


def _join(values: tuple[str, ...]) -> str:
    return "；".join(values)


def build_taxonomy_block(taxonomy: Taxonomy) -> str:
    """渲染给分类 prompt 的受控词表；只包含激活的标签。"""
    if not isinstance(taxonomy, Taxonomy):
        raise TypeError("taxonomy 必须是 Taxonomy")
    primary = [tag for tag in taxonomy.tags if tag.level is TagLevel.PRIMARY and tag.active]
    secondary = [tag for tag in taxonomy.tags if tag.level is TagLevel.SECONDARY and tag.active]
    if not primary:
        raise ClassificationError("classification.taxonomy", "已确认分类法没有主标签")
    lines = [f"分类法：{taxonomy.id} v{taxonomy.version}", "主标签："]
    for tag in primary:
        lines.append(f"- {tag.code} {tag.name}")
        if tag.definition:
            lines.append(f"  定义：{tag.definition}")
        for label, values in (("纳入", tag.include), ("排除", tag.exclude), ("正例", tag.examples)):
            if values:
                lines.append(f"  {label}：{_join(values)}")
    lines.append("副标签（可引用代码、名称或别名）：")
    for tag in secondary:
        aliases = f"；别名：{_join(tag.aliases)}" if tag.aliases else ""
        lines.append(f"- {tag.code} {tag.name}{aliases}")
    return "\n".join(lines)


def _primary_catalog(taxonomy: Taxonomy) -> dict[str, Tag]:
    return {tag.code: tag for tag in taxonomy.tags if tag.level is TagLevel.PRIMARY and tag.active}


def _secondary_index(taxonomy: Taxonomy) -> dict[str, str]:
    index: dict[str, str] = {}
    for tag in taxonomy.tags:
        if tag.level is not TagLevel.SECONDARY or not tag.active:
            continue
        for value in (tag.code, tag.name, *tag.aliases):
            if value.strip():
                index.setdefault(value.strip().casefold(), tag.code)
    return index


def _paper_text(paper: Paper) -> str:
    return "\n\n".join(part for part in (paper.title, paper.abstract) if part)


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _evidence(output: ClassificationOutput, paper_text: str) -> tuple[ClassificationEvidence, ...]:
    normalized = _normalize_text(paper_text)
    result: list[ClassificationEvidence] = []
    for item in output.evidence:
        quote = item.quote.strip()
        valid = len(quote) >= MIN_EVIDENCE_CHARS and _normalize_text(quote) in normalized
        result.append(ClassificationEvidence(quote, valid))
    return tuple(result)


def _secondary_tags(
    output: ClassificationOutput,
    taxonomy: Taxonomy,
    *,
    paper_id: str,
    primary_codes: set[str],
    provenance: Provenance,
) -> tuple[tuple[PaperTag, ...], list[str]]:
    index = _secondary_index(taxonomy)
    seen: set[str] = set()
    tags: list[PaperTag] = []
    reasons: list[str] = []
    for item in output.secondary_tags:
        name = item.name.strip()
        code = index.get(name.casefold(), name)
        if not code or len(code) > MAX_FREE_TAG_CHARS or code in primary_codes:
            reasons.append(f"invalid_secondary_tag:{name}")
            continue
        if code in seen:
            reasons.append(f"duplicate_secondary_tag:{code}")
            continue
        seen.add(code)
        tags.append(
            PaperTag(
                paper_id=paper_id,
                tag_code=code,
                role=TagRole.SECONDARY,
                taxonomy_version=taxonomy.version,
                confidence=item.confidence,
                assigned_by=AssignedBy.AGENT,
                provenance=provenance,
            )
        )
    return tuple(tags), reasons


def classify_paper(
    paper: Paper,
    taxonomy: Taxonomy,
    topic_buckets: Mapping[str, Bucket],
    gateway: LLMGateway,
) -> ClassificationResult:
    """调用 classification/v2；taxonomy 必须已确认，主标签必须有主题桶。"""
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if not isinstance(taxonomy, Taxonomy):
        raise TypeError("taxonomy 必须是 Taxonomy")
    if taxonomy.status is not DocumentStatus.CONFIRMED:
        raise ClassificationError("classification.taxonomy", "只允许使用已确认 taxonomy")
    if not isinstance(topic_buckets, Mapping):
        raise TypeError("topic_buckets 必须是 tag_code → Bucket 的映射")
    if not isinstance(gateway, LLMGateway):
        raise TypeError("gateway 必须是 LLMGateway")
    primary_catalog = _primary_catalog(taxonomy)
    if not primary_catalog:
        raise ClassificationError("classification.taxonomy", "已确认 taxonomy 没有激活主标签")

    result = gateway.complete_json(
        "classification",
        {
            "taxonomy": build_taxonomy_block(taxonomy),
            "title": paper.title,
            "abstract": paper.abstract or "（无摘要）",
        },
        prompt_version=CLASSIFICATION_PROMPT_VERSION,
    )
    if not isinstance(result.data, ClassificationOutput):
        raise ClassificationError("classification.schema", "LLM 返回了非 classification 输出")
    output = result.data

    primary = primary_catalog.get(output.primary_tag)
    if primary is None:
        raise ClassificationError(
            "classification.primary_tag", "主标签不在已确认 taxonomy 中或未激活"
        )
    bucket = topic_buckets.get(primary.code)
    if (
        not isinstance(bucket, Bucket)
        or not bucket.active
        or bucket.bucket_type is not BucketType.TOPIC
        or bucket.tag_code != primary.code
    ):
        raise ClassificationError(
            "classification.bucket", f"主标签 {primary.code} 没有可用的激活主题桶"
        )

    reasons: list[str] = []
    if not paper.abstract:
        reasons.append("missing_abstract")
    if output.primary_confidence < DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD:
        reasons.append("low_primary_confidence")
    if output.runner_up_tag is None:
        runner_up = None
    else:
        runner_up = primary_catalog.get(output.runner_up_tag)
        if runner_up is None:
            reasons.append("invalid_runner_up")
        elif output.runner_up_confidence is not None:
            margin = output.primary_confidence - output.runner_up_confidence
            if margin < DEFAULT_CLASSIFICATION_MARGIN:
                reasons.append("ambiguous_primary")
    if output.boundary_note and output.boundary_note.strip():
        reasons.append("boundary_case")

    evidence = _evidence(output, _paper_text(paper))
    if not evidence or not any(item.valid for item in evidence):
        reasons.append("missing_evidence")
    if any(not item.valid for item in evidence):
        reasons.append("invalid_evidence")

    secondary, secondary_reasons = _secondary_tags(
        output,
        taxonomy,
        paper_id=paper.id,
        primary_codes=set(primary_catalog),
        provenance=result.provenance,
    )
    reasons.extend(secondary_reasons)
    primary_tag = PaperTag(
        paper_id=paper.id,
        tag_code=primary.code,
        role=TagRole.PRIMARY,
        taxonomy_version=taxonomy.version,
        confidence=output.primary_confidence,
        assigned_by=AssignedBy.AGENT,
        provenance=result.provenance,
    )
    filing = PaperBucket(
        paper_id=paper.id,
        bucket_id=bucket.id,
        role=BucketRole.FILING,
        assigned_by=AssignedBy.AGENT,
        reason=output.boundary_note.strip() if output.boundary_note else None,
    )
    deduplicated_reasons = tuple(dict.fromkeys(reasons))
    return ClassificationResult(
        paper_id=paper.id,
        taxonomy_id=taxonomy.id,
        taxonomy_version=taxonomy.version,
        classification=output,
        primary_tag=primary_tag,
        secondary_tags=secondary,
        bucket=filing,
        evidence=evidence,
        needs_review=bool(deduplicated_reasons),
        reasons=deduplicated_reasons,
        provenance=result.provenance,
    )


def _tag_signature(tag: PaperTag) -> tuple[str, str, str, float]:
    return (tag.tag_code, tag.role.value, tag.taxonomy_version, round(tag.confidence, 6))


def persist_classification(session: Session, result: ClassificationResult) -> bool:
    """首次写入主/副标签与主题归属桶；相同结果幂等，不同结果拒绝静默覆盖。"""
    if not isinstance(session, Session):
        raise TypeError("session 必须是 Session")
    if not isinstance(result, ClassificationResult):
        raise TypeError("result 必须是 ClassificationResult")
    tags_repo = PaperTagRepository(session)
    bucket_repo = PaperBucketRepository(session)
    existing_tags = tags_repo.list_for_paper(result.paper_id)
    existing_buckets = bucket_repo.list_for_paper(result.paper_id)
    existing_filing = [item for item in existing_buckets if item.role is BucketRole.FILING]
    if existing_tags:
        existing_signature = sorted(_tag_signature(tag) for tag in existing_tags)
        desired_signature = sorted(_tag_signature(tag) for tag in result.tags)
        if existing_signature != desired_signature:
            raise ClassificationError(
                "classification.existing", "已存在不同分类；重分类需显式流程，不能静默覆盖"
            )
    if existing_filing and existing_filing[0].bucket_id != result.bucket.bucket_id:
        raise ClassificationError("classification.bucket", "已存在不同归属桶；不能静默移动")
    changed = False
    if not existing_tags:
        tags_repo.add_many(result.tags)
        changed = True
    if not existing_filing:
        bucket_repo.add(result.bucket)
        changed = True
    return changed


__all__ = [
    "CLASSIFICATION_PROMPT_VERSION",
    "ClassificationError",
    "ClassificationEvidence",
    "ClassificationResult",
    "build_taxonomy_block",
    "classify_paper",
    "persist_classification",
]
