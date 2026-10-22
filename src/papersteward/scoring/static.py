"""P4 静态分：A/E/S 确定性计算、版本冻结与静态快照。

静态分只依赖 R、A、E 与版本化评分配置；不使用 LLM。完整每日 ``score_snapshot``
（含动态分/优先级）由 P5 生成；本模块写入独立的 ``static_score_snapshot``。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import Session

from papersteward.core.entities import Paper, StaticScoreSnapshot, utc_now
from papersteward.core.enums import OaStatus
from papersteward.core.errors import DomainError
from papersteward.scoring.config import ScoringConfig
from papersteward.storage.repositories import StaticScoreSnapshotRepository

VENUE_TIERS: MappingProxyType[str, float] = MappingProxyType(
    {
        "a*": 1.0,
        "a+": 1.0,
        "a": 0.85,
        "b": 0.65,
        "c": 0.45,
        "unknown": 0.5,
        "predatory": 0.0,
    }
)
AUTHOR_MAX_WEIGHT = 0.6
AUTHOR_TOP2_WEIGHT = 0.4
YOUNG_PAPER_DAYS = 183
CITATION_NEUTRAL = 0.5
UNKNOWN_NEUTRAL = 0.5


class StaticScoreError(DomainError):
    """静态分输入/版本/持久化错误；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class AuthorityInputs:
    venue_level: str | None = None
    author_scores: tuple[float, ...] = ()
    citation_percentile: float | None = None
    published_at: datetime | None = None
    as_of: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.author_scores, tuple | list):
            raise StaticScoreError("static.author.author_scores", "必须是数值序列")
        scores = tuple(self.author_scores)
        for value in scores:
            _ratio(value, "static.author.author_scores")
        object.__setattr__(self, "author_scores", scores)
        if self.citation_percentile is not None:
            _ratio(self.citation_percentile, "static.author.citation_percentile")
        if self.published_at is not None and (
            self.published_at.tzinfo is None or self.published_at.utcoffset() is None
        ):
            raise StaticScoreError("static.author.published_at", "必须带时区")
        if self.as_of is not None and (
            isinstance(self.as_of, datetime) or not isinstance(self.as_of, date)
        ):
            raise StaticScoreError("static.author.as_of", "必须是 date，不是 datetime")


@dataclass(frozen=True, slots=True)
class AuthorityScore:
    score: float
    venue: float
    author_team: float
    citation: float
    confidence: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("venue", "author_team", "citation", "confidence"):
            _ratio(getattr(self, name), f"static.author.{name}")
        _score(self.score, "static.author.score")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.reasons
        ):
            raise StaticScoreError("static.author.reasons", "必须是非空字符串元组")


@dataclass(frozen=True, slots=True)
class ExtraValueInputs:
    code: float | None = None
    data: float | None = None
    reproducibility: float | None = None
    rigor: float | None = None
    oa_status: OaStatus = OaStatus.UNKNOWN

    def __post_init__(self) -> None:
        for name in ("code", "data", "reproducibility", "rigor"):
            value = getattr(self, name)
            if value is not None:
                _ratio(value, f"static.extra.{name}")
        if not isinstance(self.oa_status, OaStatus):
            raise StaticScoreError("static.extra.oa_status", "必须是 OaStatus")


@dataclass(frozen=True, slots=True)
class ExtraValueScore:
    score: float
    code: float
    data: float
    reproducibility: float
    rigor: float
    open: float
    confidence: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("code", "data", "reproducibility", "rigor", "open", "confidence"):
            _ratio(getattr(self, name), f"static.extra.{name}")
        _score(self.score, "static.extra.score")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.reasons
        ):
            raise StaticScoreError("static.extra.reasons", "必须是非空字符串元组")


@dataclass(frozen=True, slots=True)
class StaticScoreResult:
    relevance: float
    authority: float
    extra_value: float
    static_score: float
    confidence: float
    authority_details: AuthorityScore
    extra_value_details: ExtraValueScore
    breakdown: dict[str, Any]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("relevance", "authority", "extra_value", "static_score"):
            _score(getattr(self, name), f"static.{name}")
        _ratio(self.confidence, "static.confidence")
        if not isinstance(self.authority_details, AuthorityScore):
            raise StaticScoreError("static.authority_details", "必须是 AuthorityScore")
        if not isinstance(self.extra_value_details, ExtraValueScore):
            raise StaticScoreError("static.extra_value_details", "必须是 ExtraValueScore")
        if not isinstance(self.breakdown, dict):
            raise StaticScoreError("static.breakdown", "必须是 JSON 对象")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.reasons
        ):
            raise StaticScoreError("static.reasons", "必须是非空字符串元组")

    def as_json(self) -> dict[str, Any]:
        return {
            "relevance": self.relevance,
            "authority": self.authority,
            "extra_value": self.extra_value,
            "static_score": self.static_score,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "breakdown": self.breakdown,
        }


def _ratio(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise StaticScoreError(field, "必须在 0–1 之间")
    return float(value)


def _score(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 100.0
    ):
        raise StaticScoreError(field, "必须在 0–100 之间")
    return float(value)


def _confidence(reasons: Sequence[str]) -> float:
    return max(0.0, 1.0 - 0.2 * len(reasons))


def _venue_value(level: str | None) -> tuple[float, str | None]:
    if level is None or not level.strip():
        return UNKNOWN_NEUTRAL, "unknown_venue"
    normalized = level.strip().casefold()
    value = VENUE_TIERS.get(normalized)
    if value is None:
        return UNKNOWN_NEUTRAL, "unknown_venue"
    return value, None


def _author_team(scores: tuple[float, ...]) -> tuple[float, str | None]:
    if not scores:
        return UNKNOWN_NEUTRAL, "missing_author"
    ranked = sorted(scores, reverse=True)
    top2_mean = sum(ranked[:2]) / len(ranked[:2])
    return AUTHOR_MAX_WEIGHT * ranked[0] + AUTHOR_TOP2_WEIGHT * top2_mean, None


def _citation_value(inputs: AuthorityInputs) -> tuple[float, str | None]:
    as_of = inputs.as_of or datetime.now(UTC).date()
    if inputs.published_at is not None:
        age_days = (as_of - inputs.published_at.date()).days
        if age_days < YOUNG_PAPER_DAYS:
            return CITATION_NEUTRAL, "young_citation"
    if inputs.citation_percentile is None:
        return CITATION_NEUTRAL, "missing_citation"
    return float(inputs.citation_percentile), None


def compute_authority(inputs: AuthorityInputs) -> AuthorityScore:
    if not isinstance(inputs, AuthorityInputs):
        raise TypeError("inputs 必须是 AuthorityInputs")
    venue, venue_reason = _venue_value(inputs.venue_level)
    author, author_reason = _author_team(inputs.author_scores)
    citation, citation_reason = _citation_value(inputs)
    reasons = tuple(
        reason for reason in (venue_reason, author_reason, citation_reason) if reason is not None
    )
    return AuthorityScore(
        score=round(100.0 * (0.40 * venue + 0.40 * author + 0.20 * citation), 1),
        venue=venue,
        author_team=author,
        citation=citation,
        confidence=_confidence(reasons),
        reasons=reasons,
    )


def _open_value(status: OaStatus) -> tuple[float, str | None]:
    if status is OaStatus.OPEN:
        return 1.0, None
    if status is OaStatus.CLOSED:
        return 0.0, None
    return UNKNOWN_NEUTRAL, "unknown_open"


def compute_extra_value(inputs: ExtraValueInputs) -> ExtraValueScore:
    if not isinstance(inputs, ExtraValueInputs):
        raise TypeError("inputs 必须是 ExtraValueInputs")
    reasons: list[str] = []
    values: dict[str, float] = {}
    for name in ("code", "data", "reproducibility", "rigor"):
        value = getattr(inputs, name)
        if value is None:
            values[name] = UNKNOWN_NEUTRAL
            reasons.append(f"missing_{name}")
        else:
            values[name] = float(value)
    open_value, open_reason = _open_value(inputs.oa_status)
    if open_reason is not None:
        reasons.append(open_reason)
    result_reasons = tuple(reasons)
    return ExtraValueScore(
        score=round(
            100.0
            * (
                0.30 * values["code"]
                + 0.20 * values["data"]
                + 0.20 * values["reproducibility"]
                + 0.20 * values["rigor"]
                + 0.10 * open_value
            ),
            1,
        ),
        code=values["code"],
        data=values["data"],
        reproducibility=values["reproducibility"],
        rigor=values["rigor"],
        open=open_value,
        confidence=_confidence(result_reasons),
        reasons=result_reasons,
    )


def compute_static_score(
    relevance: float,
    authority: AuthorityScore,
    extra_value: ExtraValueScore,
    config: ScoringConfig,
) -> StaticScoreResult:
    """S = R·wR + A·wA + E·wE；权重来自已校验 ScoringConfig。"""
    if not isinstance(authority, AuthorityScore):
        raise TypeError("authority 必须是 AuthorityScore")
    if not isinstance(extra_value, ExtraValueScore):
        raise TypeError("extra_value 必须是 ExtraValueScore")
    if not isinstance(config, ScoringConfig):
        raise TypeError("config 必须是 ScoringConfig")
    _score(relevance, "static.relevance")
    weights = config.static
    total = weights.relevance + weights.authority + weights.extra_value
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise StaticScoreError("static.weights", "R/A/E 权重之和必须为 1")
    static = round(
        relevance * weights.relevance
        + authority.score * weights.authority
        + extra_value.score * weights.extra_value,
        1,
    )
    reasons = tuple(dict.fromkeys((*authority.reasons, *extra_value.reasons)))
    breakdown: dict[str, Any] = {
        "formula": "S = R*wR + A*wA + E*wE",
        "weights": {
            "relevance": weights.relevance,
            "authority": weights.authority,
            "extra_value": weights.extra_value,
        },
        "relevance": relevance,
        "authority": {
            "score": authority.score,
            "venue": authority.venue,
            "author_team": authority.author_team,
            "citation": authority.citation,
            "confidence": authority.confidence,
            "reasons": list(authority.reasons),
        },
        "extra_value": {
            "score": extra_value.score,
            "code": extra_value.code,
            "data": extra_value.data,
            "reproducibility": extra_value.reproducibility,
            "rigor": extra_value.rigor,
            "open": extra_value.open,
            "confidence": extra_value.confidence,
            "reasons": list(extra_value.reasons),
        },
        "static_score": static,
    }
    return StaticScoreResult(
        relevance=float(relevance),
        authority=authority.score,
        extra_value=extra_value.score,
        static_score=static,
        confidence=min(authority.confidence, extra_value.confidence),
        authority_details=authority,
        extra_value_details=extra_value,
        breakdown=breakdown,
        reasons=reasons,
    )


def _input_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_static_snapshot(
    result: StaticScoreResult,
    paper: Paper,
    *,
    score_date: date,
    config_version: str,
    profile_version: str,
    taxonomy_version: str,
    created_at: datetime | None = None,
) -> StaticScoreSnapshot:
    if not isinstance(result, StaticScoreResult):
        raise TypeError("result 必须是 StaticScoreResult")
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if isinstance(score_date, datetime) or not isinstance(score_date, date):
        raise StaticScoreError("static.score_date", "必须是 date，不是 datetime")
    payload = {
        "paper_id": paper.id,
        "score_date": score_date.isoformat(),
        "relevance": result.relevance,
        "authority": result.authority,
        "extra_value": result.extra_value,
        "static_score": result.static_score,
        "confidence": result.confidence,
        "config_version": config_version,
        "profile_version": profile_version,
        "taxonomy_version": taxonomy_version,
        "breakdown": result.breakdown,
    }
    return StaticScoreSnapshot(
        paper_id=paper.id,
        score_date=score_date,
        relevance=result.relevance,
        authority=result.authority,
        extra_value=result.extra_value,
        static_score=result.static_score,
        confidence=result.confidence,
        config_version=config_version,
        profile_version=profile_version,
        taxonomy_version=taxonomy_version,
        input_hash=_input_hash(payload),
        breakdown_json=result.breakdown,
        created_at=created_at or utc_now(),
    )


def persist_static_snapshot(session: Session, snapshot: StaticScoreSnapshot) -> StaticScoreSnapshot:
    """追加静态分快照；仓储负责日期/版本冻结校验，不提交事务。"""
    if not isinstance(session, Session):
        raise TypeError("session 必须是 Session")
    if not isinstance(snapshot, StaticScoreSnapshot):
        raise TypeError("snapshot 必须是 StaticScoreSnapshot")
    return StaticScoreSnapshotRepository(session).append(snapshot)


__all__ = [
    "AUTHOR_MAX_WEIGHT",
    "AUTHOR_TOP2_WEIGHT",
    "CITATION_NEUTRAL",
    "UNKNOWN_NEUTRAL",
    "VENUE_TIERS",
    "YOUNG_PAPER_DAYS",
    "AuthorityInputs",
    "AuthorityScore",
    "ExtraValueInputs",
    "ExtraValueScore",
    "StaticScoreError",
    "StaticScoreResult",
    "build_static_snapshot",
    "compute_authority",
    "compute_extra_value",
    "compute_static_score",
    "persist_static_snapshot",
]
