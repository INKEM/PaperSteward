"""按周冻结的显式评分偏好；不消费备注，不改写已读文献。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.scoring.dynamic import natural_day

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass(frozen=True)
class Feedback:
    paper_id: str
    completed_on: date
    rating: int | None
    tags: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    venue: str | None = None

    def __post_init__(self) -> None:
        natural_day(self.completed_on)
        if self.rating is not None and (
            isinstance(self.rating, bool)
            or not isinstance(self.rating, int)
            or not 1 <= self.rating <= 5
        ):
            raise DomainValidationError("feedback.rating", "必须是1–5整数或null")


@dataclass(frozen=True)
class PreferenceScore:
    preference: float
    raw: float
    confidence: float
    tag: float
    author: float
    venue: float
    ratings_count: int
    completed_count: int
    coverage: float
    cutoff: date
    reasons: tuple[str, ...]


def weekly_cutoff(as_of: date, weekday: str = "SUN") -> date:
    natural_day(as_of)
    if weekday not in WEEKDAYS:
        raise DomainValidationError("update_weekday", "未知星期")
    return as_of - timedelta(days=(as_of.weekday() - WEEKDAYS.index(weekday)) % 7)


def compute_preference(
    *,
    paper_id: str,
    primary_tag: str,
    secondary_tags: Sequence[str],
    authors: Sequence[str],
    venue: str | None,
    feedback: Sequence[Feedback],
    as_of: date,
    config: ScoringConfig,
    parents: Mapping[str, str] | None = None,
) -> PreferenceScore:
    validate_scoring_config(config)
    q = config.quasi_dynamic
    cutoff = weekly_cutoff(as_of, q.update_weekday)
    # 更新日00:00为排他截止；同一周后续打分不会即时改变U。
    history = [f for f in feedback if f.completed_on < cutoff and f.paper_id != paper_id]
    if len({f.paper_id for f in history}) != len(history):
        raise DomainValidationError("feedback", "每篇只能有一条最终阅读反馈")
    rated = [f for f in history if f.rating is not None]
    n = len(rated)
    coverage = n / len(history) if history else 0.0
    lam = q.shrinkage_lambda

    def affinity(rows: Sequence[Feedback]) -> float:
        return 50 + 25 * sum((f.rating or 3) - 3 for f in rows) / (len(rows) + lam)

    global_prior = affinity(rated)

    def tag_value(tag: str) -> float:
        rows = [f for f in rated if tag in f.tags]
        parent = (parents or {}).get(tag)
        fallback = [f for f in rated if parent and parent in f.tags]
        return affinity(rows or fallback) if rows or fallback else global_prior

    selected = list(dict.fromkeys([primary_tag, *secondary_tags[:3]]))
    tag_score = sum(tag_value(tag) for tag in selected) / len(selected)
    author_values = [affinity([f for f in rated if a in f.authors]) for a in dict.fromkeys(authors)]
    author_score = sum(author_values) / len(author_values) if author_values else 50.0
    venue_score = affinity([f for f in rated if f.venue == venue]) if venue else 50.0
    raw = tag_score * q.tag_weight + author_score * q.author_weight + venue_score * q.venue_weight
    confidence = n / (n + 10)
    reasons = []
    if coverage < 0.3:
        confidence *= coverage / 0.3
        reasons.append("insufficient_rating_coverage")
    if not n:
        reasons.append("no_ratings")
    return PreferenceScore(
        round(50 + (raw - 50) * confidence, 1),
        round(raw, 4),
        round(confidence, 4),
        round(tag_score, 4),
        round(author_score, 4),
        round(venue_score, 4),
        n,
        len(history),
        round(coverage, 4),
        cutoff,
        tuple(reasons),
    )
