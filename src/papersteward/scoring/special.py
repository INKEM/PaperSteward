"""P7-S2 特殊桶档位：确定性 rubric、V 分档、人工覆盖与配额默认值。

V 只用于特殊桶内部 high/medium/low 分档，不是 P，也不进入主队列。五维权重固定
版本化；维度分由已有提名证据、富化信号、R 与发表年龄做确定性代理，缺失信号保持
中性 50 并记录原因。该代理是 Beta 启发式，用户可用 ``override_special_tier`` 覆盖。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.core.entities import (
    EnrichmentSignals,
    NominationEvidence,
    Paper,
    SpecialNominationRecord,
    SpecialTierSnapshot,
    utc_now,
)
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    OaStatus,
    ScoreTier,
    SpecialBucketKind,
    SpecialNominationStatus,
)
from papersteward.core.errors import DomainError
from papersteward.errors import ConflictError, NotFoundError
from papersteward.storage.repositories import (
    BucketRepository,
    EnrichmentRepository,
    PaperBucketRepository,
    PaperRepository,
    SpecialNominationRepository,
    SpecialTierRepository,
    StaticScoreSnapshotRepository,
)

RUBRIC_VERSION: Final = "special_rubric/v1"
HIGH_TIER_MIN: Final = 75.0
MEDIUM_TIER_MIN: Final = 50.0

_RUBRIC_WEIGHTS: Final[dict[SpecialBucketKind, dict[str, float]]] = {
    SpecialBucketKind.REVIEW: {
        "cross_layer_depth": 0.30,
        "coverage": 0.25,
        "authority": 0.20,
        "clarity": 0.15,
        "timeliness": 0.10,
    },
    SpecialBucketKind.DISTANT: {
        "transfer_potential": 0.35,
        "bridge_clarity": 0.20,
        "credibility": 0.20,
        "novelty": 0.15,
        "accessibility": 0.10,
    },
    SpecialBucketKind.CLASSIC: {
        "foundational": 0.35,
        "long_term_impact": 0.25,
        "authority": 0.20,
        "current_relevance": 0.20,
    },
}
RUBRIC_WEIGHTS: Final[Mapping[SpecialBucketKind, Mapping[str, float]]] = MappingProxyType(
    {kind: MappingProxyType(weights) for kind, weights in _RUBRIC_WEIGHTS.items()}
)

DIMENSION_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cross_layer_depth": "跨层深度",
        "coverage": "覆盖完整度",
        "authority": "权威性",
        "clarity": "清晰度",
        "timeliness": "时效性",
        "transfer_potential": "迁移潜力",
        "bridge_clarity": "桥接清晰度",
        "credibility": "可信度",
        "novelty": "新颖性",
        "accessibility": "可及性",
        "foundational": "奠基性",
        "long_term_impact": "长期影响力",
        "current_relevance": "当前相关性",
    }
)

# 设计 §8.1 区间下界；配额是上限，不是每日必须完成的 KPI。
DEFAULT_SPECIAL_QUOTAS: Final[Mapping[SpecialBucketKind, int]] = MappingProxyType(
    {
        SpecialBucketKind.REVIEW: 3,
        SpecialBucketKind.DISTANT: 2,
        SpecialBucketKind.CLASSIC: 1,
    }
)

_BRIDGE_FIELDS: Final = (
    "source_mechanism",
    "shared_structure",
    "transfer_experiment",
    "falsification_signal",
)
_VENUE_QUALITY: Final[Mapping[str, float]] = MappingProxyType(
    {
        "a*": 100.0,
        "a+": 100.0,
        "a": 85.0,
        "b": 65.0,
        "c": 45.0,
        "predatory": 0.0,
    }
)
_CLASSIC_BASIS_SCORES: Final[Mapping[str, float]] = MappingProxyType(
    {
        "foundational": 100.0,
        "standard": 90.0,
        "award": 90.0,
        "expert": 85.0,
        "citation": 70.0,
    }
)


class SpecialTierError(DomainError):
    """档位 rubric 输入/证据错误；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class SpecialValue:
    """一次 rubric 加权结果；``contributions`` 为各维贡献，便于审计。"""

    kind: SpecialBucketKind
    value: float
    dimensions: Mapping[str, float]
    weights: Mapping[str, float]
    contributions: Mapping[str, float]

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "dimensions": dict(self.dimensions),
            "weights": dict(self.weights),
            "contributions": dict(self.contributions),
        }


@dataclass(frozen=True, slots=True)
class SpecialTierAssessment:
    """自动评估结果；``tier`` 必须与 ``value`` 的阈值一致。"""

    kind: SpecialBucketKind
    value: float
    tier: ScoreTier
    dimensions: Mapping[str, float]
    weights: Mapping[str, float]
    contributions: Mapping[str, float]
    reasons: tuple[str, ...]
    explanation: tuple[str, ...]
    evidence: tuple[NominationEvidence, ...]
    breakdown: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SpecialBucketKind):
            raise SpecialTierError("special_tier.kind", "必须是 SpecialBucketKind")
        value = round(_score(self.value, "special_tier.value"), 1)
        if not isinstance(self.tier, ScoreTier) or self.tier is not tier_for_value(value):
            raise SpecialTierError("special_tier.tier", "必须与 V 阈值一致")
        _validate_dimensions(self.kind, self.dimensions, self.weights)
        for name, expected in RUBRIC_WEIGHTS[self.kind].items():
            if not math.isclose(float(self.weights[name]), expected, abs_tol=1e-9):
                raise SpecialTierError("special_tier.weights", "必须使用冻结 rubric 权重")
        if not isinstance(self.contributions, Mapping) or set(self.contributions) != set(
            self.weights
        ):
            raise SpecialTierError("special_tier.contributions", "必须覆盖全部维度")
        for name, contribution in self.contributions.items():
            _score(contribution, f"special_tier.contributions.{name}")
        if not math.isclose(value, math.fsum(self.contributions.values()), abs_tol=0.06):
            raise SpecialTierError("special_tier.contributions", "贡献之和必须等于 V")
        for name in ("reasons", "explanation"):
            values = _text_tuple(getattr(self, name), f"special_tier.{name}")
            if not values:
                raise SpecialTierError(f"special_tier.{name}", "至少需要一条")
            object.__setattr__(self, name, values)
        if not isinstance(self.evidence, tuple | list) or not self.evidence:
            raise SpecialTierError("special_tier.evidence", "至少需要一条证据")
        evidence = tuple(self.evidence)
        for item in evidence:
            if not isinstance(item, NominationEvidence):
                raise SpecialTierError("special_tier.evidence[]", "必须是 NominationEvidence")
        object.__setattr__(self, "evidence", evidence)
        if not isinstance(self.breakdown, Mapping):
            raise SpecialTierError("special_tier.breakdown", "必须是 JSON 对象")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "weights", dict(self.weights))
        object.__setattr__(self, "contributions", dict(self.contributions))
        object.__setattr__(self, "breakdown", dict(self.breakdown))

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "tier": self.tier.value,
            "dimensions": dict(self.dimensions),
            "weights": dict(self.weights),
            "contributions": dict(self.contributions),
            "reasons": list(self.reasons),
            "explanation": list(self.explanation),
            "evidence": [{"quote": item.quote, "valid": item.valid} for item in self.evidence],
            "breakdown": dict(self.breakdown),
            "rubric_version": RUBRIC_VERSION,
        }


def tier_for_value(value: float) -> ScoreTier:
    """V≥75 high；50≤V<75 medium；V<50 low。先按一位小数取 V 再分档。"""
    score = round(_score(value, "special_tier.value"), 1)
    if score >= HIGH_TIER_MIN:
        return ScoreTier.HIGH
    if score >= MEDIUM_TIER_MIN:
        return ScoreTier.MEDIUM
    return ScoreTier.LOW


def compute_special_value(kind: SpecialBucketKind, dimensions: Mapping[str, float]) -> SpecialValue:
    """按 kind 的冻结权重计算一位小数 V；键/范围不符即拒绝。"""
    if not isinstance(kind, SpecialBucketKind):
        raise SpecialTierError("special_tier.kind", "必须是 SpecialBucketKind")
    weights = RUBRIC_WEIGHTS[kind]
    _validate_dimensions(kind, dimensions, weights)
    values = {name: _score(dimensions[name], f"special_tier.dimensions.{name}") for name in weights}
    contributions = {name: round(weights[name] * values[name], 4) for name in weights}
    value = round(math.fsum(weights[name] * values[name] for name in weights), 1)
    return SpecialValue(
        kind=kind,
        value=value,
        dimensions=values,
        weights=dict(weights),
        contributions=contributions,
    )


def assess_nomination(
    nomination: SpecialNominationRecord,
    paper: Paper,
    *,
    enrichment: EnrichmentSignals | None = None,
    relevance: float | None = None,
    as_of: date | None = None,
) -> SpecialTierAssessment:
    """纯函数：由提名/论文/富化/R 推导五维并分档；不写库、不调用模型。"""
    if not isinstance(nomination, SpecialNominationRecord):
        raise TypeError("nomination 必须是 SpecialNominationRecord")
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if paper.id != nomination.paper_id:
        raise SpecialTierError("special_tier.paper_id", "提名与论文必须一致")
    if enrichment is not None and not isinstance(enrichment, EnrichmentSignals):
        raise SpecialTierError("special_tier.enrichment", "必须是 EnrichmentSignals 或 None")
    if relevance is not None:
        relevance = _score(relevance, "special_tier.relevance")
    day = _as_date(as_of or utc_now().date())
    signals = enrichment or EnrichmentSignals()
    age_days = _age_days(paper.published_at, day)

    if nomination.kind is SpecialBucketKind.REVIEW:
        dimensions, reasons, explanation, inputs = _review_dimensions(
            nomination, paper, signals, relevance, age_days
        )
    elif nomination.kind is SpecialBucketKind.DISTANT:
        dimensions, reasons, explanation, inputs = _distant_dimensions(
            nomination, paper, signals, relevance, age_days
        )
    else:
        dimensions, reasons, explanation, inputs = _classic_dimensions(
            nomination, paper, signals, relevance, age_days
        )
    value = compute_special_value(nomination.kind, dimensions)
    breakdown = {
        "rubric_version": RUBRIC_VERSION,
        "inputs": inputs,
        "contributions": dict(value.contributions),
    }
    return SpecialTierAssessment(
        kind=nomination.kind,
        value=value.value,
        tier=tier_for_value(value.value),
        dimensions=value.dimensions,
        weights=value.weights,
        contributions=value.contributions,
        reasons=tuple(dict.fromkeys(reasons)),
        explanation=tuple(dict.fromkeys(explanation)),
        evidence=nomination.evidence,
        breakdown=breakdown,
    )


def assess_special(
    session: Session,
    paper_id: str,
    *,
    kind: SpecialBucketKind | None = None,
    nomination: SpecialNominationRecord | None = None,
    as_of: date | None = None,
    now: datetime | None = None,
) -> SpecialTierSnapshot:
    """评估一篇已接受的特殊桶文献并追加快照；不改变 P/Filing。"""
    timestamp = now or utc_now()
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    resolved = nomination or _accepted_nomination(session, paper_id, kind)
    if resolved.paper_id != paper_id:
        raise SpecialTierError("special_tier.paper_id", "提名与论文必须一致")
    if resolved.status is not SpecialNominationStatus.ACCEPTED:
        raise ConflictError("只有已接受的特殊桶提名才能评估档位")
    _ensure_special_filing(session, paper_id, resolved.kind)
    enrichment = EnrichmentRepository(session).get(paper_id)
    static = StaticScoreSnapshotRepository(session).latest(paper_id)
    assessment = assess_nomination(
        resolved,
        paper,
        enrichment=None if enrichment is None else enrichment.signals,
        relevance=None if static is None else static.relevance,
        as_of=as_of or timestamp.date(),
    )
    snapshot = SpecialTierSnapshot(
        id=uuid4().hex,
        paper_id=paper_id,
        nomination_id=resolved.id,
        kind=assessment.kind,
        value=assessment.value,
        tier=assessment.tier,
        dimensions=assessment.dimensions,
        weights=assessment.weights,
        reasons=assessment.reasons,
        explanation=assessment.explanation,
        evidence=assessment.evidence,
        breakdown_json=assessment.breakdown,
        rubric_version=RUBRIC_VERSION,
        assigned_by=AssignedBy.AGENT,
        created_at=timestamp,
    )
    return SpecialTierRepository(session).append(snapshot)


def override_special_tier(
    session: Session,
    paper_id: str,
    *,
    tier: ScoreTier,
    reviewed_by: str,
    note: str,
    now: datetime | None = None,
) -> SpecialTierSnapshot:
    """人工改档：追加新快照，保留自动 V 与旧档位历史。"""
    if not isinstance(tier, ScoreTier):
        raise SpecialTierError("special_tier.tier", "必须是 ScoreTier")
    if not reviewed_by.strip():
        raise ConflictError("必须记录审核人")
    if not note.strip():
        raise ConflictError("人工改档必须提供备注")
    repository = SpecialTierRepository(session)
    latest = repository.latest(paper_id)
    if latest is None:
        raise ConflictError("尚无档位评估；请先运行 shelf assess")
    if latest.tier is tier:
        raise ConflictError("新档位与当前档位相同")
    snapshot = SpecialTierSnapshot(
        id=uuid4().hex,
        paper_id=paper_id,
        nomination_id=latest.nomination_id,
        kind=latest.kind,
        value=latest.value,
        tier=tier,
        dimensions=latest.dimensions,
        weights=latest.weights,
        reasons=tuple(dict.fromkeys((*latest.reasons, "user_override"))),
        explanation=(
            *latest.explanation,
            f"用户将档位从 {latest.tier.value} 改为 {tier.value}。",
        ),
        evidence=latest.evidence,
        breakdown_json=latest.breakdown_json,
        rubric_version=latest.rubric_version,
        assigned_by=AssignedBy.USER,
        note=note,
        created_at=now or utc_now(),
    )
    return repository.append(snapshot)


def _accepted_nomination(
    session: Session, paper_id: str, kind: SpecialBucketKind | None
) -> SpecialNominationRecord:
    accepted = [
        record
        for record in SpecialNominationRepository(session).list_for_paper(paper_id)
        if record.status is SpecialNominationStatus.ACCEPTED
        and (kind is None or record.kind is kind)
    ]
    if not accepted:
        raise ConflictError("文献没有已接受的特殊桶提名")
    if len(accepted) > 1:
        raise ConflictError("同一文献存在多个已接受的特殊桶提名")
    return accepted[0]


def _ensure_special_filing(session: Session, paper_id: str, kind: SpecialBucketKind) -> None:
    filings = [
        item
        for item in PaperBucketRepository(session).list_for_paper(paper_id)
        if item.role is BucketRole.FILING
    ]
    if len(filings) != 1:
        raise ConflictError("文献缺少唯一归属桶")
    bucket = BucketRepository(session).get(filings[0].bucket_id)
    if bucket is None or bucket.bucket_type is not BucketType.SPECIAL:
        raise ConflictError("文献当前未归属特殊桶")
    if bucket.special_kind is not kind:
        raise ConflictError("特殊桶归属与提名类型不一致")


def _review_dimensions(
    nomination: SpecialNominationRecord,
    paper: Paper,
    enrichment: EnrichmentSignals,
    relevance: float | None,
    age_days: int | None,
) -> tuple[dict[str, float], tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    del relevance  # review rubric 不使用 R；保留统一签名
    layers = tuple(dict.fromkeys(nomination.review_layers))
    valid, total = _evidence_counts(nomination)
    ratio = valid / total if total else 0.0
    reasons: list[str] = [f"review_layers:{len(layers)}"]
    explanation: list[str] = [
        f"覆盖 {len(layers)} 个层级（{', '.join(layers) if layers else '无'}）。"
    ]
    depth = {0: 0.0, 1: 40.0, 2: 65.0, 3: 80.0, 4: 90.0, 5: 95.0}.get(len(layers), 100.0)
    if ratio == 1.0 and total:
        depth += 5.0
        reasons.append("evidence_all_valid")
    if {"method", "evaluation"} <= set(layers):
        depth += 5.0
        reasons.append("method_evaluation_bridge")
    cross_layer_depth = _clamp(depth)

    coverage = _clamp(
        40.0
        + 15.0 * min(valid, 3)
        + 30.0 * nomination.confidence
        + (5.0 if len(layers) >= 3 else 0.0)
    )
    reasons.extend([f"evidence:{valid}/{total}", f"confidence:{nomination.confidence:.2f}"])
    if len(layers) >= 3:
        reasons.append("layers_3plus")
    explanation.append(f"{valid}/{total} 条证据可核对，提名置信度 {nomination.confidence:.2f}。")

    authority, authority_reasons, authority_text = _authority(enrichment, age_days, label="权威性")
    reasons.extend(authority_reasons)
    explanation.append(authority_text)

    clarity = _clamp(
        50.0 + 30.0 * nomination.confidence + 20.0 * ratio - 10.0 * len(nomination.reasons)
    )
    if nomination.reasons:
        reasons.append(f"gate_reasons:{len(nomination.reasons)}")
        explanation.append(f"有 {len(nomination.reasons)} 项提名门禁未通过，清晰度下调。")

    timeliness, time_reason, time_text = _recency(age_days)
    reasons.append(time_reason)
    explanation.append(time_text)
    dimensions = {
        "cross_layer_depth": round(cross_layer_depth, 1),
        "coverage": round(coverage, 1),
        "authority": round(authority, 1),
        "clarity": round(clarity, 1),
        "timeliness": round(timeliness, 1),
    }
    inputs = _base_inputs(nomination, enrichment, relevance=None, age_days=age_days)
    inputs["review_layers"] = list(layers)
    inputs["oa_status"] = _resolved_oa(enrichment, paper.oa_status).value
    return dimensions, tuple(reasons), tuple(explanation), inputs


def _distant_dimensions(
    nomination: SpecialNominationRecord,
    paper: Paper,
    enrichment: EnrichmentSignals,
    relevance: float | None,
    age_days: int | None,
) -> tuple[dict[str, float], tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    memo = nomination.bridge_memo or {}
    fields = [name for name in _BRIDGE_FIELDS if _nonempty(memo.get(name))]
    valid, total = _evidence_counts(nomination)
    ratio = valid / total if total else 0.0
    reasons: list[str] = [f"bridge_fields:{len(fields)}"]
    explanation = [f"桥接假设 {len(fields)}/4 个字段完整。"]
    missing = [name for name in _BRIDGE_FIELDS if name not in fields]
    for name in missing:
        reasons.append(f"missing_bridge:{name}")
    if missing:
        explanation.append("缺失桥接字段：" + ", ".join(missing) + "。")

    transfer = _clamp(
        40.0 + 15.0 * len(fields) + 15.0 * nomination.confidence - 10.0 * len(nomination.reasons)
    )
    clarity = _clamp(50.0 + 10.0 * len(fields) + 20.0 * ratio - 5.0 * len(nomination.reasons))
    reasons.extend([f"evidence:{valid}/{total}", f"confidence:{nomination.confidence:.2f}"])
    explanation.append(f"{valid}/{total} 条证据可核对，提名置信度 {nomination.confidence:.2f}。")
    if nomination.reasons:
        reasons.append(f"gate_reasons:{len(nomination.reasons)}")
        explanation.append(f"有 {len(nomination.reasons)} 项提名门禁未通过，迁移潜力下调。")

    authority, authority_reasons, authority_text = _authority(enrichment, None, label="可信度")
    reasons.extend(authority_reasons)
    credibility = _clamp(0.8 * authority + 20.0 * ratio)
    explanation.append(authority_text)

    novelty, novelty_reasons, novelty_text = _novelty(relevance)
    reasons.extend(novelty_reasons)
    explanation.append(novelty_text)
    access, access_reason, access_text = _accessibility_from(enrichment, paper.oa_status)
    reasons.append(access_reason)
    explanation.append(access_text)
    dimensions = {
        "transfer_potential": round(transfer, 1),
        "bridge_clarity": round(clarity, 1),
        "credibility": round(credibility, 1),
        "novelty": round(novelty, 1),
        "accessibility": round(access, 1),
    }
    inputs = _base_inputs(nomination, enrichment, relevance=relevance, age_days=age_days)
    inputs["bridge_fields"] = fields
    inputs["oa_status"] = (
        enrichment.oa_status if enrichment.oa_status is not OaStatus.UNKNOWN else paper.oa_status
    ).value
    return dimensions, tuple(reasons), tuple(explanation), inputs


def _classic_dimensions(
    nomination: SpecialNominationRecord,
    paper: Paper,
    enrichment: EnrichmentSignals,
    relevance: float | None,
    age_days: int | None,
) -> tuple[dict[str, float], tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    basis = tuple(dict.fromkeys(nomination.classic_basis))
    reasons: list[str] = []
    if basis:
        reasons.append("classic_basis:" + ",".join(basis))
        known = [_CLASSIC_BASIS_SCORES.get(item, 50.0) for item in basis]
        foundational = _clamp(max(known) + 5.0 * (len(basis) - 1))
        unknown = [item for item in basis if item not in _CLASSIC_BASIS_SCORES]
        if unknown:
            reasons.append("unknown_classic_basis:" + ",".join(unknown))
        explanation = [f"经典依据：{', '.join(basis)}。"]
    else:
        foundational = 40.0
        reasons.append("missing_classic_basis")
        explanation = ["未提供经典/奠基依据，奠基性按偏低处理。"]

    impact, impact_reasons, impact_text = _long_term_impact(enrichment.citation_count, age_days)
    reasons.extend(impact_reasons)
    explanation.append(impact_text)
    authority, authority_reasons, authority_text = _authority(enrichment, age_days, label="权威性")
    reasons.extend(authority_reasons)
    explanation.append(authority_text)
    if relevance is None:
        current = 50.0
        reasons.append("missing_relevance")
        explanation.append("当前相关度未知，按中性 50 处理。")
    else:
        current = _score(relevance, "special_tier.relevance")
        reasons.append(f"relevance:{current:.1f}")
        explanation.append(f"当前相关度 R={current:.1f}。")
    dimensions = {
        "foundational": round(foundational, 1),
        "long_term_impact": round(impact, 1),
        "authority": round(authority, 1),
        "current_relevance": round(current, 1),
    }
    inputs = _base_inputs(nomination, enrichment, relevance=relevance, age_days=age_days)
    inputs["classic_basis"] = list(basis)
    inputs["oa_status"] = _resolved_oa(enrichment, paper.oa_status).value
    return dimensions, tuple(reasons), tuple(explanation), inputs


def _authority(
    enrichment: EnrichmentSignals, age_days: int | None, *, label: str
) -> tuple[float, tuple[str, ...], str]:
    level = (enrichment.venue_level or "").strip().casefold()
    if level in _VENUE_QUALITY:
        venue = _VENUE_QUALITY[level]
        reasons = [f"venue:{level}"]
    else:
        venue = 50.0
        reasons = ["unknown_venue"]
    count = enrichment.citation_count
    if count is None:
        citation = 50.0
        reasons.append("missing_citation")
    elif age_days is not None and age_days < 365:
        citation = 50.0
        reasons.append("young_citation")
    else:
        citation = _clamp(25.0 * math.log10(1 + count))
        reasons.append(f"citations:{count}")
    score = round(0.6 * venue + 0.4 * citation, 1)
    citation_text = "未知" if count is None else f"{count} 次"
    text = f"{label}代理：venue={level or '未知'}，引用={citation_text}。"
    return score, tuple(reasons), text


def _long_term_impact(
    count: int | None, age_days: int | None
) -> tuple[float, tuple[str, ...], str]:
    if count is None:
        return 50.0, ("missing_citation",), "引用数未知，长期影响力按中性 50 处理。"
    citations = _clamp(22.0 * math.log10(1 + count))
    reasons: list[str] = [f"citations:{count}"]
    if age_days is None:
        reasons.append("missing_age")
    else:
        years = age_days / 365.25
        longevity = min(20.0, max(0.0, (years - 2.0) * 2.0))
        citations = _clamp(citations + longevity)
        reasons.append(f"longevity_years:{years:.1f}")
    return (
        round(citations, 1),
        tuple(reasons),
        f"引用 {count} 次，发表年龄 {age_days if age_days is not None else '未知'} 天。",
    )


def _novelty(relevance: float | None) -> tuple[float, tuple[str, ...], str]:
    if relevance is None:
        return 50.0, ("missing_relevance",), "当前相关度未知，新颖性按中性 50 处理。"
    if 40.0 <= relevance <= 70.0:
        return (
            100.0,
            ("relevance_distant_band",),
            f"R={relevance:.1f} 位于远邻合理带（40–70）。",
        )
    if 30.0 <= relevance < 40.0 or 70.0 < relevance <= 80.0:
        return (
            75.0,
            ("relevance_near_band",),
            f"R={relevance:.1f} 接近远邻带边缘。",
        )
    return (
        50.0,
        ("relevance_out_of_band",),
        f"R={relevance:.1f} 不在远邻合理带内，新颖性按中性 50 处理。",
    )


def _resolved_oa(enrichment: EnrichmentSignals, fallback: OaStatus) -> OaStatus:
    if enrichment.oa_status is OaStatus.UNKNOWN:
        return fallback
    return enrichment.oa_status


def _accessibility_from(
    enrichment: EnrichmentSignals, fallback: OaStatus
) -> tuple[float, str, str]:
    status = _resolved_oa(enrichment, fallback)
    base = {OaStatus.OPEN: 100.0, OaStatus.CLOSED: 30.0, OaStatus.UNKNOWN: 50.0}[status]
    score = base
    parts: list[str] = [f"OA={status.value}"]
    if enrichment.code_url:
        score += 20.0
        parts.append("有代码")
    if enrichment.data_url:
        score += 10.0
        parts.append("有数据")
    return _clamp(score), f"oa:{status.value}", "可及性：" + "，".join(parts) + "。"


def _recency(age_days: int | None) -> tuple[float, str, str]:
    if age_days is None:
        return 50.0, "missing_age", "发表时间未知，时效性按中性 50 处理。"
    if age_days <= 548:
        return 100.0, "recent_2y", f"发表约 {age_days} 天前（2 年内）。"
    if age_days <= 1095:
        return 80.0, "recent_3y", f"发表约 {age_days} 天前（3 年内）。"
    if age_days <= 1825:
        return 55.0, "older_5y", f"发表约 {age_days} 天前（5 年内）。"
    return 30.0, "stale_5y", f"发表约 {age_days} 天前（超过 5 年）。"


def _base_inputs(
    nomination: SpecialNominationRecord,
    enrichment: EnrichmentSignals,
    *,
    relevance: float | None,
    age_days: int | None,
) -> dict[str, Any]:
    valid, total = _evidence_counts(nomination)
    return {
        "evidence_valid": valid,
        "evidence_total": total,
        "confidence": nomination.confidence,
        "gate_reasons": list(nomination.reasons),
        "venue_level": enrichment.venue_level,
        "citation_count": enrichment.citation_count,
        "oa_status": enrichment.oa_status.value,
        "code": enrichment.code_url is not None,
        "data": enrichment.data_url is not None,
        "age_days": age_days,
        "relevance": relevance,
    }


def _evidence_counts(nomination: SpecialNominationRecord) -> tuple[int, int]:
    total = len(nomination.evidence)
    valid = sum(1 for item in nomination.evidence if item.valid)
    return valid, total


def _age_days(published_at: datetime | None, as_of: date) -> int | None:
    if published_at is None:
        return None
    return max(0, (as_of - published_at.date()).days)


def _validate_dimensions(
    kind: SpecialBucketKind, dimensions: Mapping[str, float], weights: Mapping[str, float]
) -> None:
    if not isinstance(dimensions, Mapping):
        raise SpecialTierError("special_tier.dimensions", "必须是 JSON 对象")
    expected = RUBRIC_WEIGHTS[kind]
    if set(dimensions) != set(expected) or set(weights) != set(expected):
        raise SpecialTierError(
            "special_tier.dimensions",
            f"{kind.value} 维度必须为 {', '.join(expected)}",
        )
    for name in expected:
        _score(dimensions[name], f"special_tier.dimensions.{name}")
        _ratio(weights[name], f"special_tier.weights.{name}")
    if not math.isclose(math.fsum(float(weights[name]) for name in expected), 1.0, abs_tol=1e-6):
        raise SpecialTierError("special_tier.weights", "权重之和必须为 1")


def _score(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 100.0
    ):
        raise SpecialTierError(field, "必须是 0–100 的有限数")
    return float(value)


def _ratio(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise SpecialTierError(field, "必须在 0–1 之间")
    return float(value)


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, tuple | list):
        raise SpecialTierError(field, "必须是字符串元组")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SpecialTierError(f"{field}[]", "必须是非空字符串")
        result.append(item.strip())
    return tuple(result)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _as_date(value: date) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise SpecialTierError("special_tier.as_of", "必须是 date，不是 datetime")
    return value


__all__ = [
    "DEFAULT_SPECIAL_QUOTAS",
    "DIMENSION_LABELS",
    "HIGH_TIER_MIN",
    "MEDIUM_TIER_MIN",
    "RUBRIC_VERSION",
    "RUBRIC_WEIGHTS",
    "SpecialTierAssessment",
    "SpecialTierError",
    "SpecialValue",
    "assess_nomination",
    "assess_special",
    "compute_special_value",
    "override_special_tier",
    "tier_for_value",
]
