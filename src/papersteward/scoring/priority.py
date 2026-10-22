"""P、EMA、日变化限制及稳定并列排序。"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.scoring.dynamic import number


class RankConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    rank_ema: float = Field(default=0.7, ge=0, le=1)
    max_daily_delta: float = Field(default=8, ge=0, le=100)
    tie_break: tuple[Literal["relevance", "published_at", "authority", "title"], ...] = (
        "relevance",
        "published_at",
        "authority",
        "title",
    )


@dataclass(frozen=True)
class PriorityScore:
    priority: float
    rank_score: float
    raw_ema: float
    delta_limited: bool
    reset: bool


def component_priority(scores: dict[str, float], weights: dict[str, float]) -> float:
    """独立组件算术，用于设计示例golden；生产使用分层priority。"""
    if not scores or scores.keys() != weights.keys() or abs(sum(weights.values()) - 1) > 1e-6:
        raise DomainValidationError("priority.weights", "分项须匹配且权重和为1")
    for name in scores:
        number(scores[name], name, 0, 100)
        number(weights[name], name, 0, 1)
    return round(sum(scores[k] * weights[k] for k in scores), 1)


def compute_priority(
    static: float,
    dynamic: float,
    preference: float,
    config: ScoringConfig,
    *,
    previous_rank: float | None = None,
    reset: bool = False,
) -> PriorityScore:
    validate_scoring_config(config)
    p = component_priority(
        {"S": static, "D": dynamic, "U": preference},
        {"S": config.static.weight, "D": config.dynamic.weight, "U": config.quasi_dynamic.weight},
    )
    if previous_rank is None or reset:
        return PriorityScore(p, p, p, False, True)
    number(previous_rank, "previous_rank", 0, 100)
    alpha = config.stability.rank_ema
    raw = alpha * p + (1 - alpha) * previous_rank
    cap = config.stability.max_daily_delta
    rank = max(previous_rank - cap, min(previous_rank + cap, raw))
    return PriorityScore(p, round(rank, 1), round(raw, 1), abs(raw - previous_rank) > cap, False)


def rank_key(
    *,
    rank_score: float,
    relevance: float,
    published_on: date | None,
    authority: float,
    title: str,
    paper_id: str,
) -> tuple[float, float, int, float, str, str]:
    for name, value in (
        ("rank_score", rank_score),
        ("relevance", relevance),
        ("authority", authority),
    ):
        number(value, name, 0, 100)
    return (
        -rank_score,
        -relevance,
        -(published_on or date.min).toordinal(),
        -authority,
        title,
        paper_id,
    )
