"""P4 相关度 R：profile facets → 语义相似度 + rubric/证据 → R/band/review。

公式（设计 §10.2.1）::

    sem = 0.7·max_sim + 0.3·mean_top2_sim
    rubric = 0.35·problem + 0.25·method + 0.25·domain + 0.15·actionability
    R = 100·(0.40·sem + 0.60·rubric/5)

语义相似度默认未校准；真实分位数/isotonic 校准需要历史标注，调用方可注入
``calibrator``。rubric 的每个维度必须有逐字来自标题/摘要的证据；缺失或伪造证据
使用中性 2.5 并标记 ``needs_review``，不静默丢弃文献。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from papersteward.core.entities import Paper, Provenance, ResearchProfile
from papersteward.core.enums import DocumentStatus, RelevanceBand
from papersteward.core.errors import DomainError
from papersteward.llm import LLMGateway
from papersteward.pipeline.embeddings import EmbeddingProvider, cosine_similarity
from papersteward.schemas import RelevanceOutput

RubricDimension = Literal["problem", "method", "domain", "actionability"]
RubricCalibrator = Callable[[float], float]
RUBRIC_DIMENSIONS: tuple[RubricDimension, ...] = (
    "problem",
    "method",
    "domain",
    "actionability",
)
RUBRIC_WEIGHTS: Mapping[RubricDimension, float] = MappingProxyType(
    {"problem": 0.35, "method": 0.25, "domain": 0.25, "actionability": 0.15}
)
NEUTRAL_RUBRIC_SCORE = 2.5
DEFAULT_MIN_RUBRIC_CONFIDENCE = 0.6
MIN_EVIDENCE_CHARS = 4
BELOW_THRESHOLD = 55.0
LOW_PRIORITY_THRESHOLD = 65.0


class RelevanceError(DomainError):
    """R 计算的输入/证据/配置错误；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class ProfileFacet:
    """用于语义相似度的单一 profile 片段。"""

    facet_id: str
    kind: str
    text: str

    def __post_init__(self) -> None:
        for name in ("facet_id", "kind", "text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RelevanceError(f"relevance.facet.{name}", "不能为空")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class SemanticScore:
    """语义相似度分量；``calibrated=False`` 表示使用原始余弦。"""

    max_similarity: float
    top2_mean: float
    score: float
    model_id: str
    matched_facet_ids: tuple[str, ...]
    calibrated: bool

    def __post_init__(self) -> None:
        for name in ("max_similarity", "top2_mean", "score"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise RelevanceError(f"relevance.semantic.{name}", "必须在 0–1 之间")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise RelevanceError("relevance.semantic.model_id", "不能为空")
        if not isinstance(self.matched_facet_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.matched_facet_ids
        ):
            raise RelevanceError("relevance.semantic.matched_facet_ids", "必须是字符串元组")
        if not isinstance(self.calibrated, bool):
            raise RelevanceError("relevance.semantic.calibrated", "必须是布尔值")


@dataclass(frozen=True, slots=True)
class RubricEvidence:
    dimension: RubricDimension
    quote: str
    valid: bool

    def __post_init__(self) -> None:
        if self.dimension not in RUBRIC_DIMENSIONS:
            raise RelevanceError("relevance.rubric.evidence.dimension", "未知维度")
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise RelevanceError("relevance.rubric.evidence.quote", "不能为空")
        if not isinstance(self.valid, bool):
            raise RelevanceError("relevance.rubric.evidence.valid", "必须是布尔值")
        object.__setattr__(self, "quote", self.quote.strip())


@dataclass(frozen=True, slots=True)
class RubricAssessment:
    """LLM rubric 结果；缺失/伪造证据已转为中性值和 review 原因。"""

    scores: Mapping[RubricDimension, int]
    evidence: tuple[RubricEvidence, ...]
    missing_dimensions: tuple[RubricDimension, ...]
    invalid_quotes: tuple[str, ...]
    confidence: float
    provenance: Provenance
    min_confidence: float = DEFAULT_MIN_RUBRIC_CONFIDENCE

    def __post_init__(self) -> None:
        normalized: dict[RubricDimension, int] = {}
        for dimension in RUBRIC_DIMENSIONS:
            value = self.scores.get(dimension)
            if type(value) is not int or not 0 <= value <= 5:
                raise RelevanceError(f"relevance.rubric.{dimension}", "必须是 0–5 的整数")
            normalized[dimension] = value
        object.__setattr__(self, "scores", MappingProxyType(normalized))
        for dimension in self.missing_dimensions:
            if dimension not in RUBRIC_DIMENSIONS:
                raise RelevanceError("relevance.rubric.missing", "未知维度")
        if len(set(self.missing_dimensions)) != len(self.missing_dimensions):
            raise RelevanceError("relevance.rubric.missing", "不能重复")
        for quote in self.invalid_quotes:
            if not isinstance(quote, str) or not quote.strip():
                raise RelevanceError("relevance.rubric.invalid_quotes", "不能为空")
        for name in ("confidence", "min_confidence"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise RelevanceError(f"relevance.rubric.{name}", "必须在 0–1 之间")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.provenance, Provenance):
            raise RelevanceError("relevance.rubric.provenance", "必须是 Provenance")

    @property
    def effective_scores(self) -> dict[RubricDimension, float]:
        return {
            dimension: (
                NEUTRAL_RUBRIC_SCORE
                if dimension in self.missing_dimensions
                else float(self.scores[dimension])
            )
            for dimension in RUBRIC_DIMENSIONS
        }

    @property
    def weighted_score(self) -> float:
        scores = self.effective_scores
        return sum(scores[dimension] * RUBRIC_WEIGHTS[dimension] for dimension in RUBRIC_DIMENSIONS)

    @property
    def review_reasons(self) -> tuple[str, ...]:
        reasons = [f"missing_evidence:{dimension}" for dimension in self.missing_dimensions]
        if self.invalid_quotes:
            reasons.append("invalid_evidence")
        if self.confidence < self.min_confidence:
            reasons.append("low_rubric_confidence")
        return tuple(reasons)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    """一次 R 评估；不改变 PaperStatus，也不写数据库。"""

    paper_id: str
    profile_id: str
    profile_version: str
    R: float
    band: RelevanceBand
    semantic: SemanticScore
    rubric: RubricAssessment
    needs_review: bool
    reasons: tuple[str, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        for name in ("paper_id", "profile_id", "profile_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RelevanceError(f"relevance.{name}", "不能为空")
            object.__setattr__(self, name, value.strip())
        if (
            isinstance(self.R, bool)
            or not isinstance(self.R, int | float)
            or not math.isfinite(self.R)
            or not 0.0 <= self.R <= 100.0
        ):
            raise RelevanceError("relevance.R", "必须在 0–100 之间")
        object.__setattr__(self, "R", round(float(self.R), 1))
        if not isinstance(self.band, RelevanceBand):
            raise RelevanceError("relevance.band", "必须是 RelevanceBand")
        if not isinstance(self.semantic, SemanticScore):
            raise RelevanceError("relevance.semantic", "必须是 SemanticScore")
        if not isinstance(self.rubric, RubricAssessment):
            raise RelevanceError("relevance.rubric", "必须是 RubricAssessment")
        if not isinstance(self.needs_review, bool):
            raise RelevanceError("relevance.needs_review", "必须是布尔值")
        if not isinstance(self.provenance, Provenance):
            raise RelevanceError("relevance.provenance", "必须是 Provenance")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reasons
        ):
            raise RelevanceError("relevance.reasons", "必须是字符串元组")

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "R": self.R,
            "band": self.band.value,
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
            "semantic": {
                "max_similarity": round(self.semantic.max_similarity, 6),
                "top2_mean": round(self.semantic.top2_mean, 6),
                "score": round(self.semantic.score, 6),
                "model_id": self.semantic.model_id,
                "calibrated": self.semantic.calibrated,
                "matched_facet_ids": list(self.semantic.matched_facet_ids),
            },
            "rubric": {
                "scores": dict(self.rubric.scores),
                "effective_scores": self.rubric.effective_scores,
                "weighted_score": round(self.rubric.weighted_score, 3),
                "confidence": self.rubric.confidence,
                "missing_dimensions": list(self.rubric.missing_dimensions),
                "invalid_quotes": list(self.rubric.invalid_quotes),
                "evidence": [
                    {"dimension": item.dimension, "quote": item.quote, "valid": item.valid}
                    for item in self.rubric.evidence
                ],
            },
            "provenance": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "generated_at": self.provenance.generated_at.isoformat(),
                "input_hash": self.provenance.input_hash,
            },
        }


def _text_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def extract_profile_facets(profile: ResearchProfile) -> tuple[ProfileFacet, ...]:
    """把已确认 profile 拆成稳定顺序的语义片段；空档案明确拒绝。"""
    if not isinstance(profile, ResearchProfile):
        raise TypeError("profile 必须是 ResearchProfile")
    parsed = profile.parsed_json
    facets: list[ProfileFacet] = []

    positioning = _mapping(parsed.get("positioning"))
    summary = positioning.get("summary")
    if isinstance(summary, str) and summary.strip():
        facets.append(ProfileFacet("positioning", "positioning", summary))

    questions = parsed.get("core_questions")
    if isinstance(questions, Sequence) and not isinstance(questions, str):
        for index, item in enumerate(questions, start=1):
            entry = _mapping(item)
            identifier = str(entry.get("id") or f"Q{index}")
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                facets.append(ProfileFacet(f"question-{identifier}", "question", text))

    subdirections = parsed.get("subdirections")
    if isinstance(subdirections, Sequence) and not isinstance(subdirections, str):
        for index, item in enumerate(subdirections, start=1):
            entry = _mapping(item)
            identifier = str(entry.get("id") or f"S{index}")
            parts: list[str] = []
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                parts.append(name.strip())
            keywords = _text_items(entry.get("keywords"))
            if keywords:
                parts.append("关键词：" + "、".join(keywords))
            description = entry.get("description")
            if isinstance(description, str) and description.strip():
                parts.append(description.strip())
            works = _text_items(entry.get("representative_work"))
            if works:
                parts.append("代表工作：" + "；".join(works))
            if parts:
                facets.append(
                    ProfileFacet(f"subdirection-{identifier}", "subdirection", "；".join(parts))
                )

    for kind in ("methods", "data", "include"):
        for index, text in enumerate(_text_items(parsed.get(kind)), start=1):
            facets.append(ProfileFacet(f"{kind}-{index}", kind, text))

    deduplicated: list[ProfileFacet] = []
    seen: set[tuple[str, str]] = set()
    for facet in facets:
        key = (facet.kind, facet.text.casefold())
        if key not in seen:
            seen.add(key)
            deduplicated.append(facet)
    if not deduplicated:
        raise RelevanceError("relevance.profile.facets", "profile 没有可用于相关度的文本")
    return tuple(deduplicated)


def build_paper_text(paper: Paper) -> str:
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if not paper.title.strip():
        raise RelevanceError("relevance.paper.title", "不能为空")
    return "\n\n".join(
        part for part in (paper.title, paper.abstract) if isinstance(part, str) and part.strip()
    )


def build_profile_block(profile: ResearchProfile) -> str:
    """渲染给 rubric prompt 的 profile 文本；只使用已确认的解析结果。"""
    if not isinstance(profile, ResearchProfile):
        raise TypeError("profile 必须是 ResearchProfile")
    parsed = profile.parsed_json
    lines: list[str] = []
    positioning = _mapping(parsed.get("positioning"))
    if summary := positioning.get("summary"):
        lines.append(f"定位：{summary}")
    questions = parsed.get("core_questions")
    if isinstance(questions, Sequence) and not isinstance(questions, str):
        for index, item in enumerate(questions, start=1):
            entry = _mapping(item)
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(f"核心问题 {entry.get('id') or index}：{text.strip()}")
    subdirections = parsed.get("subdirections")
    if isinstance(subdirections, Sequence) and not isinstance(subdirections, str):
        for index, item in enumerate(subdirections, start=1):
            entry = _mapping(item)
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            details = [f"子方向 {entry.get('id') or index}：{name.strip()}"]
            keywords = _text_items(entry.get("keywords"))
            if keywords:
                details.append("关键词：" + "、".join(keywords))
            description = entry.get("description")
            if isinstance(description, str) and description.strip():
                details.append(description.strip())
            lines.append("；".join(details))
    for kind in ("methods", "data", "include", "exclude"):
        values = _text_items(parsed.get(kind))
        if values:
            lines.append(f"{kind}：" + "；".join(values))
    if not lines:
        raise RelevanceError("relevance.profile", "profile 没有可用于 rubric 的文本")
    return "\n".join(lines)


def score_semantic(
    paper: Paper,
    facets: Sequence[ProfileFacet],
    provider: EmbeddingProvider,
    *,
    calibrator: RubricCalibrator | None = None,
) -> SemanticScore:
    """计算 max/top2 语义分量；负余弦按 0 处理，未提供校准器时明确标记。"""
    if not facets:
        raise RelevanceError("relevance.semantic.facets", "至少需要一个 profile 片段")
    vectors = provider.embed([facet.text for facet in facets] + [build_paper_text(paper)])
    expected = len(facets) + 1
    if len(vectors) != expected or any(len(vector) != len(vectors[0]) for vector in vectors):
        raise RelevanceError("relevance.semantic.embedding", "embedding 数量或维度不一致")
    paper_vector = vectors[-1]
    ranked = sorted(
        (
            (facet.facet_id, max(0.0, cosine_similarity(vector, paper_vector)))
            for facet, vector in zip(facets, vectors[:-1], strict=True)
        ),
        key=lambda item: (-item[1], item[0]),
    )
    top = ranked[:2]
    max_similarity = top[0][1] if top else 0.0
    top2_mean = sum(score for _, score in top) / len(top) if top else 0.0
    raw = 0.7 * max_similarity + 0.3 * top2_mean
    calibrated = calibrator is not None
    score = calibrator(raw) if calibrator is not None else raw
    if (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
    ):
        raise RelevanceError("relevance.semantic.calibrator", "校准结果必须在 0–1 之间")
    return SemanticScore(
        max_similarity=max_similarity,
        top2_mean=top2_mean,
        score=float(score),
        model_id=provider.model_id,
        matched_facet_ids=tuple(facet_id for facet_id, _ in ranked),
        calibrated=calibrated,
    )


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def score_rubric(
    gateway: LLMGateway,
    profile: ResearchProfile,
    paper: Paper,
    *,
    min_confidence: float = DEFAULT_MIN_RUBRIC_CONFIDENCE,
) -> RubricAssessment:
    """调用 relevance prompt；证据必须逐字存在于标题/摘要，否则中性降级并转 review。"""
    if not isinstance(profile, ResearchProfile):
        raise TypeError("profile 必须是 ResearchProfile")
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if (
        isinstance(min_confidence, bool)
        or not isinstance(min_confidence, int | float)
        or not math.isfinite(min_confidence)
        or not 0.0 <= min_confidence <= 1.0
    ):
        raise RelevanceError("relevance.rubric.min_confidence", "必须在 0–1 之间")
    result = gateway.complete_json(
        "relevance",
        {
            "profile": build_profile_block(profile),
            "title": paper.title,
            "abstract": paper.abstract or "（无摘要）",
        },
    )
    if not isinstance(result.data, RelevanceOutput):
        raise RelevanceError("relevance.rubric.schema", "LLM 返回了非 relevance 输出")
    output = result.data
    paper_text = _normalize_text(build_paper_text(paper))
    evidence: list[RubricEvidence] = []
    for item in output.evidence:
        quote = item.quote.strip()
        valid = len(quote) >= MIN_EVIDENCE_CHARS and _normalize_text(quote) in paper_text
        evidence.append(RubricEvidence(item.dimension, quote, valid))
    valid_dimensions = {item.dimension for item in evidence if item.valid}
    missing = tuple(
        dimension for dimension in RUBRIC_DIMENSIONS if dimension not in valid_dimensions
    )
    invalid_quotes = tuple(item.quote for item in evidence if not item.valid)
    return RubricAssessment(
        scores={
            "problem": output.problem,
            "method": output.method,
            "domain": output.domain,
            "actionability": output.actionability,
        },
        evidence=tuple(evidence),
        missing_dimensions=missing,
        invalid_quotes=invalid_quotes,
        confidence=output.confidence,
        provenance=result.provenance,
        min_confidence=min_confidence,
    )


def compute_r(semantic_score: float, rubric_weighted: float) -> float:
    """R = 100·(0.40·sem + 0.60·rubric/5)，保留一位小数。"""
    if (
        isinstance(semantic_score, bool)
        or not isinstance(semantic_score, int | float)
        or not math.isfinite(semantic_score)
        or not 0.0 <= semantic_score <= 1.0
    ):
        raise RelevanceError("relevance.R.semantic", "必须在 0–1 之间")
    if (
        isinstance(rubric_weighted, bool)
        or not isinstance(rubric_weighted, int | float)
        or not math.isfinite(rubric_weighted)
        or not 0.0 <= rubric_weighted <= 5.0
    ):
        raise RelevanceError("relevance.R.rubric", "必须在 0–5 之间")
    return round(100.0 * (0.40 * semantic_score + 0.60 * rubric_weighted / 5.0), 1)


def band_for_r(value: float) -> RelevanceBand:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 100.0
    ):
        raise RelevanceError("relevance.band", "R 必须在 0–100 之间")
    if value < BELOW_THRESHOLD:
        return RelevanceBand.BELOW_THRESHOLD
    if value < LOW_PRIORITY_THRESHOLD:
        return RelevanceBand.LOW
    return RelevanceBand.NORMAL


def evaluate_relevance(
    profile: ResearchProfile,
    paper: Paper,
    gateway: LLMGateway,
    embedding_provider: EmbeddingProvider,
    *,
    calibrator: RubricCalibrator | None = None,
    min_rubric_confidence: float = DEFAULT_MIN_RUBRIC_CONFIDENCE,
) -> RelevanceResult:
    """完整 R 评估；只接受已确认 profile，失败向上抛出可审计错误。"""
    if not isinstance(profile, ResearchProfile):
        raise TypeError("profile 必须是 ResearchProfile")
    if profile.status is not DocumentStatus.CONFIRMED:
        raise RelevanceError("relevance.profile.status", "只允许使用已确认 profile")
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    facets = extract_profile_facets(profile)
    semantic = score_semantic(paper, facets, embedding_provider, calibrator=calibrator)
    rubric = score_rubric(gateway, profile, paper, min_confidence=min_rubric_confidence)
    value = compute_r(semantic.score, rubric.weighted_score)
    reasons = list(rubric.review_reasons)
    if not paper.abstract:
        reasons.append("missing_abstract")
    if not semantic.calibrated:
        reasons.append("uncalibrated_similarity")
    return RelevanceResult(
        paper_id=paper.id,
        profile_id=profile.id,
        profile_version=profile.version,
        R=value,
        band=band_for_r(value),
        semantic=semantic,
        rubric=rubric,
        needs_review=rubric.needs_review or not bool(paper.abstract),
        reasons=tuple(dict.fromkeys(reasons)),
        provenance=rubric.provenance,
    )


__all__ = [
    "BELOW_THRESHOLD",
    "DEFAULT_MIN_RUBRIC_CONFIDENCE",
    "LOW_PRIORITY_THRESHOLD",
    "NEUTRAL_RUBRIC_SCORE",
    "RUBRIC_DIMENSIONS",
    "RUBRIC_WEIGHTS",
    "ProfileFacet",
    "RelevanceError",
    "RelevanceResult",
    "RubricAssessment",
    "RubricEvidence",
    "SemanticScore",
    "band_for_r",
    "build_paper_text",
    "build_profile_block",
    "compute_r",
    "evaluate_relevance",
    "extract_profile_facets",
    "score_rubric",
    "score_semantic",
]
