"""固定自然日上的 F/H/D；不访问数据库或时钟。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import ScoringConfig, validate_scoring_config


def number(value: float, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise DomainValidationError(field, f"必须是 {low}–{high} 的有限数")
    return float(value)


def natural_day(value: date, field: str = "date") -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise DomainValidationError(field, "必须是自然日 date")
    return value


def target_shares(
    primary_tags: Sequence[str],
    shares: Mapping[str, float] | None = None,
    *,
    subdirection_to_tag: Mapping[str, str] | None = None,
) -> dict[str, float]:
    tags = tuple(primary_tags)
    if not tags or len(set(tags)) != len(tags):
        raise DomainValidationError("target_shares", "主标签必须非空且不重复")
    if shares is None:
        return dict.fromkeys(tags, 1 / len(tags))
    for value in shares.values():
        number(value, "target_shares", 0, 1)
    if not math.isclose(sum(shares.values()), 1, abs_tol=1e-6):
        raise DomainValidationError("target_shares", "占比之和必须为1")
    result = dict.fromkeys(tags, 0.0)
    for source, value in shares.items():
        tag = source if subdirection_to_tag is None else subdirection_to_tag.get(source)
        if tag not in result:
            raise DomainValidationError("target_shares", "存在未映射的子方向/未知主标签")
        assert tag is not None
        result[tag] += value
    if subdirection_to_tag is None and set(shares) != set(tags):
        raise DomainValidationError("target_shares", "占比须覆盖全部主标签")
    return result


@dataclass(frozen=True)
class Completion:
    primary_tag: str
    completed_on: date


@dataclass(frozen=True)
class DynamicScore:
    freshness: float
    hunger: float
    dynamic_score: float
    age_days: int | None
    share: float
    target_share: float
    completed_count: int
    total_completed: int
    backlog_dampened: bool
    reasons: tuple[str, ...]


def freshness(published_on: date | None, as_of: date, half_life: int = 180) -> float:
    natural_day(as_of)
    if isinstance(half_life, bool) or half_life <= 0:
        raise DomainValidationError("half_life", "必须为正数")
    if published_on is None:
        return 50.0
    age = max(0, (as_of - natural_day(published_on)).days)
    return round(5 + 95 * math.exp(-math.log(2) * age / half_life), 1)


def compute_dynamic(
    *,
    primary_tag: str,
    published_on: date | None,
    as_of: date,
    shares: Mapping[str, float],
    completions: Sequence[Completion],
    config: ScoringConfig,
    backlog: int = 0,
    weekly_capacity: int = 10,
    backlog_dampener: bool = True,
    half_life: int | None = None,
) -> DynamicScore:
    validate_scoring_config(config)
    natural_day(as_of)
    resolved = target_shares(tuple(shares), shares)
    if primary_tag not in resolved:
        raise DomainValidationError("primary_tag", "缺少目标占比")
    if backlog < 0 or weekly_capacity <= 0:
        raise DomainValidationError("backlog", "积压须非负，周阅读容量须大于0")
    start = as_of - timedelta(days=config.dynamic.hunger_window_days - 1)
    recent = [c for c in completions if start <= natural_day(c.completed_on) <= as_of]
    if any(c.primary_tag not in resolved for c in recent):
        raise DomainValidationError("completions", "阅读历史包含未知主标签")
    total = len(recent)
    count = sum(c.primary_tag == primary_tag for c in recent)
    target = resolved[primary_tag]
    alpha = config.dynamic.prior_strength
    share = target if total == 0 else (count + alpha * target) / (total + alpha)
    gap = max(-1.0, min(1.0, (target - share) / max(target, 0.05)))
    h = 50 + 50 * gap
    dampened = backlog_dampener and backlog > 2 * weekly_capacity and h > 50
    if dampened:
        h = 50 + (h - 50) / 2
    f = freshness(published_on, as_of, half_life or config.dynamic.half_life_days.default)
    reasons = () if published_on else ("missing_publication_date",)
    return DynamicScore(
        f,
        round(h, 1),
        round(f * config.dynamic.freshness + h * config.dynamic.hunger, 1),
        None if published_on is None else max(0, (as_of - published_on).days),
        share,
        target,
        count,
        total,
        dampened,
        reasons,
    )
