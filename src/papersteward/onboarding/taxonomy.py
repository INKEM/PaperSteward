"""G2 分类法审阅：正文为源、证据追加写、影响只预览。"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from difflib import unified_diff
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import Event, Taxonomy
from papersteward.core.enums import DocumentStatus, EventType, TagLevel
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage
from papersteward.documents import confirm_taxonomy, diff_taxonomies, parse_taxonomy, render_diff
from papersteward.documents.diff import taxonomy_diff_payload
from papersteward.documents.frontmatter import load_yaml_mapping, split_front_matter
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.profile import MAX_PROFILE_BYTES, load_profile, parse_draft
from papersteward.onboarding.service import _atomic, confirm_gate
from papersteward.storage.models import EventRow, PaperRow, PaperTagRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import EventRepository, TaxonomyRepository


class SeedLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    paper_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    primary_tag: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _evidence_id(taxonomy_id: str, version: str) -> str:
    return f"evt-taxonomy-draft-{_hash([taxonomy_id, version])[:32]}"


def _g1_binding(session: Session) -> dict[str, Any]:
    event = session.scalar(
        select(EventRow)
        .where(EventRow.event_type == EventType.ONBOARDING_G1_CONFIRMED.value)
        .order_by(EventRow.created_at.desc(), EventRow.id.desc())
        .limit(1)
    )
    if event is None:
        raise InvariantViolation("onboarding.g1", "缺少 G1 确认事件，请先完成档案确认")
    profile = load_profile(
        session, event.payload_json["artifact_id"], event.payload_json["artifact_version"]
    )
    parsed = parse_draft(profile.content_md, profile.id)
    if (
        profile.status is not DocumentStatus.CONFIRMED
        or parsed.version != profile.version
        or dict(parsed.parsed_json) != dict(profile.parsed_json)
    ):
        raise InvariantViolation("onboarding.g1", "G1 档案已变化或不再确认，请先处理档案再协商")
    return {
        "event_id": event.id,
        "id": profile.id,
        "version": profile.version,
        "content_hash": _content_hash(profile.content_md),
    }


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


def _boundaries(taxonomy: Taxonomy) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    cases, issues = [], []
    current = None
    for line in taxonomy.content_md.splitlines():
        if line.startswith("## "):
            heading = re.match(r"^## ([A-Z][A-Z0-9_-]{1,31})\s", line)
            current = None if heading is None else heading[1]
        if current and re.match(r"^[-*]\s*边界[：:]", line.strip()):
            match = re.fullmatch(
                r"[-*]\s*边界[：:]\s*(.+?)\s*[；;]\s*判为[：:]\s*([A-Z][A-Z0-9_-]{1,31})\s*[；;]\s*理由[：:]\s*(.+)",
                line.strip(),
            )
            if match and match[1].strip() and match[3].strip():
                cases.append(
                    {
                        "source_tag": current,
                        "case": match[1].strip(),
                        "target_tag": match[2],
                        "reason": match[3].strip(),
                    }
                )
            else:
                issues.append(
                    {
                        "field": f"taxonomy.{current}.boundaries",
                        "message": "边界需包含案例；判为：Txx；理由：说明",
                    }
                )
    return cases, issues


def validate_taxonomy(taxonomy: Taxonomy, seeds: list[SeedLabel]) -> dict[str, Any]:
    if len({seed.paper_id for seed in seeds}) != len(seeds):
        raise DomainValidationError("taxonomy.seeds", "种子 paper_id 不得重复")
    primary = [tag for tag in taxonomy.tags if tag.level is TagLevel.PRIMARY and tag.active]
    codes = {tag.code for tag in primary}
    cases, issues = _boundaries(taxonomy)
    if not 5 <= len(primary) <= 8:
        issues.append({"field": "taxonomy.tags", "message": "G2 需要 5–8 个主标签"})
    counts = Counter(seed.primary_tag for seed in seeds if seed.primary_tag in codes)
    total, covered = len(seeds), sum(counts.values())
    if not total or covered * 10 < total * 9:
        issues.append(
            {"field": "taxonomy.coverage", "message": "种子覆盖率须 ≥90%，空清单不能验证"}
        )
    maximum = max(counts.values(), default=0)
    if total and maximum * 2 > total:
        issues.append({"field": "taxonomy.balance", "message": "单标签占比须 ≤50%"})
    if any(seed.primary_tag is not None and seed.primary_tag not in codes for seed in seeds):
        issues.append({"field": "taxonomy.seeds", "message": "标注引用未知/非启用主标签"})
    if any(case["target_tag"] not in codes for case in cases):
        issues.append({"field": "taxonomy.boundaries", "message": "边界判定须引用启用主标签"})
    for tag in primary:
        sentences = [part for part in re.split(r"[。！？.!?]+", tag.definition) if part.strip()]
        if not 2 <= len(sentences) <= 4:
            issues.append({"field": f"taxonomy.{tag.code}.definition", "message": "定义需 2–4 句"})
        examples = {
            seed.paper_id
            for seed in seeds
            if seed.primary_tag == tag.code
            and (seed.paper_id in tag.examples or seed.title in tag.examples)
        }
        if len(set(tag.examples)) < 3 or len(examples) < 3:
            issues.append(
                {
                    "field": f"taxonomy.{tag.code}.examples",
                    "message": "需至少 3 个不同且匹配种子标注的正例",
                }
            )
        if len({case["case"] for case in cases if case["source_tag"] == tag.code}) < 2:
            issues.append(
                {
                    "field": f"taxonomy.{tag.code}.boundaries",
                    "message": "需至少 2 个不同的边界案例及判定理由",
                }
            )
    return {
        "g2_ready": not issues,
        "issues": issues,
        "seed_count": total,
        "primary_tag_count": len(primary),
        "coverage": covered / total if total else 0,
        "max_tag_share": maximum / total if total else 0,
        "distribution": [
            {
                "tag_code": tag.code,
                "count": counts[tag.code],
                "share": counts[tag.code] / total if total else 0,
                "paper_ids": [seed.paper_id for seed in seeds if seed.primary_tag == tag.code],
            }
            for tag in primary
        ],
        "unclassified": [seed.paper_id for seed in seeds if seed.primary_tag not in codes],
        "boundaries": cases,
    }


def _load(session: Session, taxonomy_id: str, version: str) -> Taxonomy:
    taxonomy = TaxonomyRepository(session).get(taxonomy_id, version)
    if taxonomy is None:
        raise NotFoundError("分类法版本不存在", details={"id": taxonomy_id, "version": version})
    return taxonomy


def _evidence(session: Session, taxonomy: Taxonomy) -> dict[str, Any]:
    event = EventRepository(session).get(_evidence_id(taxonomy.id, taxonomy.version))
    if event is None:
        raise ConflictError("缺少版本化种子证据，请通过初始化 API 提交新草案版本")
    return dict(event.payload_json)


def taxonomy_review(session: Session, taxonomy_id: str, version: str) -> dict[str, Any]:
    taxonomy = _load(session, taxonomy_id, version)
    proof = _evidence(session, taxonomy)
    if proof["profile"] != _g1_binding(session):
        raise InvariantViolation("onboarding.g1", "草案绑定的档案已变化，请重新协商")
    parsed = parse_taxonomy_draft(taxonomy.content_md, taxonomy.id)
    if (
        parsed.version != taxonomy.version
        or sorted(parsed.tags, key=lambda t: t.code) != sorted(taxonomy.tags, key=lambda t: t.code)
        or _content_hash(taxonomy.content_md) != proof["content_hash"]
    ):
        raise InvariantViolation("taxonomy.content", "分类法正文、标签缓存或审阅证据不一致")
    seeds = [SeedLabel.model_validate(seed) for seed in proof["seeds"]]
    report = validate_taxonomy(parsed, seeds)
    base = (
        None
        if proof["base_version"] is None
        else _load(session, taxonomy.id, proof["base_version"])
    )
    previous_event = (
        None if base is None else EventRepository(session).get(_evidence_id(base.id, base.version))
    )
    old_seeds = (
        {}
        if previous_event is None
        else {
            seed["paper_id"]: seed["primary_tag"] for seed in previous_event.payload_json["seeds"]
        }
    )
    old_tags = {} if base is None else taxonomy_diff_payload(base)["tags"]
    new_tags = taxonomy_diff_payload(parsed)["tags"]
    old_cases = (
        {}
        if base is None
        else {(case["source_tag"], case["case"]): case for case in _boundaries(base)[0]}
    )
    new_cases = {(case["source_tag"], case["case"]): case for case in report["boundaries"]}
    boundary_changed = {
        key[0]
        for key in old_cases.keys() | new_cases.keys()
        if old_cases.get(key) != new_cases.get(key)
    }
    changed = sorted(
        code
        for code in old_tags.keys() | new_tags.keys()
        if old_tags.get(code) != new_tags.get(code) or code in boundary_changed
    )
    labels = {seed.paper_id: seed.primary_tag for seed in seeds}
    rows = session.execute(
        select(PaperRow, PaperTagRow)
        .join(PaperTagRow, PaperTagRow.paper_id == PaperRow.id)
        .where(PaperRow.status.in_(["queued", "scored", "snoozed"]), PaperTagRow.role == "primary")
        .order_by(PaperRow.id)
    ).all()
    queue = [
        {
            "paper_id": paper.id,
            "title": paper.title,
            "from_tag": tag.tag_code,
            "to_tag": labels.get(paper.id),
            "needs_review": True,
        }
        for paper, tag in rows
        if (base is None or tag.taxonomy_version == base.version)
        and (tag.tag_code in changed or (paper.id in labels and labels[paper.id] != tag.tag_code))
    ]
    report.update(
        {
            "profile": proof["profile"],
            "sample_kind": proof["sample_kind"],
            "sample_notice": "仅流程验证"
            if proof["sample_kind"] == "synthetic"
            else "基于用户提供的种子标注",
            "seed_changes": [
                {
                    "paper_id": seed.paper_id,
                    "from_tag": old_seeds.get(seed.paper_id),
                    "to_tag": seed.primary_tag,
                }
                for seed in seeds
                if old_seeds.get(seed.paper_id) != seed.primary_tag
            ],
            "boundary_changes": [
                {
                    "source_tag": key[0],
                    "case": key[1],
                    "before": old_cases.get(key),
                    "after": new_cases.get(key),
                }
                for key in sorted(old_cases.keys() | new_cases.keys())
                if old_cases.get(key) != new_cases.get(key)
            ],
            "queue_impact": queue,
            "queue_notice": "按标签代码/版本筛选（无 taxonomy_id），待人工复核；未执行重分类",
            "query_rubric_impact": [
                {
                    "tag_code": code,
                    "before": old_tags.get(code),
                    "after": new_tags.get(code),
                    "boundary_rules_changed": code in boundary_changed,
                }
                for code in changed
            ],
            "base_version": proof["base_version"],
            "baseline_seed_labels_available": previous_event is not None,
            "baseline_seed_count": len(old_seeds) if previous_event is not None else None,
            "removed_seed_ids": sorted(old_seeds.keys() - labels.keys()),
        }
    )
    return {
        "id": taxonomy.id,
        "version": taxonomy.version,
        "status": taxonomy.status.value,
        "content_md": taxonomy.content_md,
        "content_hash": proof["content_hash"],
        "review_hash": _hash(report),
        "report": report,
        "diff": None if base is None else render_diff(diff_taxonomies(base, parsed)),
        "markdown_diff": None
        if base is None
        else "\n".join(
            unified_diff(
                base.content_md.splitlines(),
                parsed.content_md.splitlines(),
                fromfile=base.version,
                tofile=parsed.version,
                lineterm="",
            )
        ),
    }


def save_taxonomy_draft(
    session: Session,
    content_md: str,
    taxonomy_id: str,
    seeds: list[SeedLabel],
    *,
    sample_kind: Literal["user", "synthetic"],
    base_version: str | None,
    occurred_at: datetime,
) -> dict[str, Any]:
    if get_onboarding(session).stage is not OnboardingStage.TAXONOMY:
        raise InvariantViolation("onboarding.stage", "仅 G1 后的分类法协商阶段允许编辑")
    binding = _g1_binding(session)
    taxonomy = parse_taxonomy_draft(content_md, taxonomy_id)
    validate_taxonomy(taxonomy, seeds)
    repository = TaxonomyRepository(session)
    if repository.get(taxonomy.id, taxonomy.version) is not None:
        raise ConflictError("分类法版本已存在；编辑请提升 version")
    if base_version is None and repository.list(taxonomy.id):
        raise ConflictError("编辑已有分类法必须指定 base_version")
    if base_version is not None:
        _load(session, taxonomy.id, base_version)
    with _atomic(session):
        repository.add(taxonomy)
        EventRepository(session).append(
            Event(
                id=_evidence_id(taxonomy.id, taxonomy.version),
                event_type=EventType.ONBOARDING_TAXONOMY_DRAFTED,
                created_at=occurred_at,
                payload_json={
                    "profile": binding,
                    "taxonomy_id": taxonomy.id,
                    "version": taxonomy.version,
                    "content_hash": _content_hash(taxonomy.content_md),
                    "seeds": [seed.model_dump() for seed in seeds],
                    "sample_kind": sample_kind,
                    "base_version": base_version,
                },
            )
        )
        review = taxonomy_review(session, taxonomy.id, taxonomy.version)
    return review


def confirm_g2(
    session: Session,
    approval: GateApproval,
    *,
    content_hash: str,
    review_hash: str,
    expected_revision: int,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    evidence: dict[str, Any] = {}

    def validate(session: Session, state: Onboarding, gate: Gate, approval: GateApproval) -> None:
        review = taxonomy_review(session, approval.artifact_id, approval.artifact_version)
        if review["status"] == DocumentStatus.ARCHIVED.value:
            raise InvariantViolation("taxonomy.status", "已归档分类法不能通过 G2")
        if review["content_hash"] != content_hash or review["review_hash"] != review_hash:
            raise InvariantViolation("taxonomy.review_hash", "分类法或影响预览已变化，请重新审阅")
        if not review["report"]["g2_ready"]:
            issue = review["report"]["issues"][0]
            raise DomainValidationError(issue["field"], issue["message"])
        evidence.update(
            {
                "draft_event_id": _evidence_id(approval.artifact_id, approval.artifact_version),
                "profile": review["report"]["profile"],
                "review_hash": review_hash,
                "sample_kind": review["report"]["sample_kind"],
                "report": review["report"],
            }
        )
        if review["status"] == DocumentStatus.DRAFT.value:
            confirm_taxonomy(
                session, approval.artifact_id, approval.artifact_version, confirmed_at=occurred_at
            )

    return confirm_gate(
        session,
        Gate.G2,
        approval,
        expected_revision=expected_revision,
        validate_evidence=validate,
        event_id=event_id,
        occurred_at=occurred_at,
        evidence_json=evidence,
    )
