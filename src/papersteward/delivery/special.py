"""P7-S2 特殊桶配额与每日轮换计划。

纯计划不写库、不发送：每日最多两个槽位，按 review→classic→distant 循环轮换；
配额是每周上限而非 KPI，空桶/低档/配额用尽时跳过，不跨桶比较 V。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy.orm import Session

from papersteward.core.enums import (
    BucketRole,
    BucketType,
    PaperStatus,
    ScoreTier,
    SpecialBucketKind,
)
from papersteward.core.errors import DomainError
from papersteward.scoring.special import DEFAULT_SPECIAL_QUOTAS
from papersteward.storage.repositories import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    SpecialTierRepository,
)

SPECIAL_ROTATION_ORDER: Final = (
    SpecialBucketKind.REVIEW,
    SpecialBucketKind.CLASSIC,
    SpecialBucketKind.DISTANT,
)
_TIER_RANK: Final[Mapping[ScoreTier, int]] = MappingProxyType(
    {ScoreTier.HIGH: 0, ScoreTier.MEDIUM: 1, ScoreTier.LOW: 2}
)
_ELIGIBLE_TIERS: Final = frozenset({ScoreTier.HIGH, ScoreTier.MEDIUM})


class SpecialRotationError(DomainError):
    """轮换计划输入错误；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class SpecialCandidate:
    """一个已评估、待安排的特殊桶候选。"""

    paper_id: str
    title: str
    kind: SpecialBucketKind
    tier: ScoreTier
    value: float

    def __post_init__(self) -> None:
        if not isinstance(self.paper_id, str) or not self.paper_id.strip():
            raise SpecialRotationError("special_rotation.paper_id", "不能为空")
        if not isinstance(self.title, str) or not self.title.strip():
            raise SpecialRotationError("special_rotation.title", "不能为空")
        if not isinstance(self.kind, SpecialBucketKind):
            raise SpecialRotationError("special_rotation.kind", "必须是 SpecialBucketKind")
        if not isinstance(self.tier, ScoreTier):
            raise SpecialRotationError("special_rotation.tier", "必须是 ScoreTier")
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int | float)
            or not math.isfinite(self.value)
            or not 0.0 <= self.value <= 100.0
        ):
            raise SpecialRotationError("special_rotation.value", "必须是 0–100 的有限数")
        object.__setattr__(self, "value", float(self.value))


@dataclass(frozen=True, slots=True)
class RotationSlot:
    """计划中的一个特殊槽位；V 不进入展示契约。"""

    paper_id: str
    title: str
    kind: SpecialBucketKind
    tier: ScoreTier

    def as_json(self) -> dict[str, str]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "kind": self.kind.value,
            "tier": self.tier.value,
        }


@dataclass(frozen=True, slots=True)
class SpecialRotationPlan:
    """某日的特殊槽位计划；``used`` 是计划后的当周投影用量。"""

    day: date
    slots: tuple[RotationSlot, ...]
    quotas: Mapping[SpecialBucketKind, int]
    used: Mapping[SpecialBucketKind, int]
    skipped: tuple[str, ...]
    max_slots: int = 2

    def as_json(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "max_slots": self.max_slots,
            "slots": [slot.as_json() for slot in self.slots],
            "quotas": {kind.value: value for kind, value in self.quotas.items()},
            "used": {kind.value: value for kind, value in self.used.items()},
            "skipped": list(self.skipped),
        }


def plan_special_rotation(
    candidates: Sequence[SpecialCandidate],
    *,
    day: date,
    quotas: Mapping[SpecialBucketKind, int] | None = None,
    used: Mapping[SpecialBucketKind, int] | None = None,
    max_slots: int = 2,
    eligible_tiers: frozenset[ScoreTier] | set[ScoreTier] = _ELIGIBLE_TIERS,
) -> SpecialRotationPlan:
    """确定性轮换：起始桶由日期序号决定；同桶每日最多一个槽位。"""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise SpecialRotationError("special_rotation.day", "必须是 date，不是 datetime")
    if isinstance(max_slots, bool) or not isinstance(max_slots, int) or not 0 <= max_slots <= 2:
        raise SpecialRotationError("special_rotation.max_slots", "必须在 0–2 之间")
    if not isinstance(eligible_tiers, set | frozenset) or not eligible_tiers:
        raise SpecialRotationError("special_rotation.eligible_tiers", "至少允许一个档位")
    for tier in eligible_tiers:
        if not isinstance(tier, ScoreTier):
            raise SpecialRotationError("special_rotation.eligible_tiers", "必须是 ScoreTier")
    quota_map = _normalize_quotas(quotas)
    used_map = _normalize_used(used)
    ids = [candidate.paper_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise SpecialRotationError("special_rotation.paper_id", "候选 paper_id 不能重复")

    eligible = [candidate for candidate in candidates if candidate.tier in eligible_tiers]
    by_kind: dict[SpecialBucketKind, list[SpecialCandidate]] = {}
    for candidate in eligible:
        by_kind.setdefault(candidate.kind, []).append(candidate)
    for group in by_kind.values():
        group.sort(key=lambda item: (_TIER_RANK[item.tier], -item.value, item.paper_id))

    offset = day.toordinal() % len(SPECIAL_ROTATION_ORDER)
    order = SPECIAL_ROTATION_ORDER[offset:] + SPECIAL_ROTATION_ORDER[:offset]
    projected = dict(used_map)
    slots: list[RotationSlot] = []
    skipped: list[str] = []
    for kind in order:
        if len(slots) >= max_slots:
            break
        if projected[kind] >= quota_map[kind]:
            skipped.append(f"quota:{kind.value}")
            continue
        group = by_kind.get(kind, [])
        if not group:
            skipped.append(
                "empty:" + kind.value
                if not any(candidate.kind is kind for candidate in candidates)
                else "low_tier:" + kind.value
            )
            continue
        candidate = group[0]
        slots.append(
            RotationSlot(
                paper_id=candidate.paper_id,
                title=candidate.title,
                kind=candidate.kind,
                tier=candidate.tier,
            )
        )
        projected[kind] += 1
    return SpecialRotationPlan(
        day=day,
        slots=tuple(slots),
        quotas=MappingProxyType(quota_map),
        used=MappingProxyType(projected),
        skipped=tuple(skipped),
        max_slots=max_slots,
    )


def load_special_candidates(
    session: Session, *, as_of: date | None = None
) -> list[SpecialCandidate]:
    """读取 shelved、当前归属特殊桶且有档位快照的候选。"""
    day = as_of or datetime.now(UTC).date()
    if isinstance(day, datetime) or not isinstance(day, date):
        raise SpecialRotationError("special_rotation.as_of", "必须是 date")
    ceiling = datetime.combine(day, time.max, tzinfo=UTC)
    candidates: list[SpecialCandidate] = []
    for snapshot in SpecialTierRepository(session).latest_all(as_of=ceiling):
        paper = PaperRepository(session).get(snapshot.paper_id)
        if paper is None or paper.status is not PaperStatus.SHELVED:
            continue
        filings = [
            item
            for item in PaperBucketRepository(session).list_for_paper(paper.id)
            if item.role is BucketRole.FILING
        ]
        if len(filings) != 1:
            continue
        bucket = BucketRepository(session).get(filings[0].bucket_id)
        if bucket is None or bucket.bucket_type is not BucketType.SPECIAL:
            continue
        if bucket.special_kind is not snapshot.kind:
            continue
        candidates.append(
            SpecialCandidate(
                paper_id=paper.id,
                title=paper.title,
                kind=snapshot.kind,
                tier=snapshot.tier,
                value=snapshot.value,
            )
        )
    return candidates


def load_special_quotas(session: Session) -> dict[SpecialBucketKind, int]:
    """读取桶配额；未配置的桶使用设计下界默认值。"""
    quotas = dict(DEFAULT_SPECIAL_QUOTAS)
    for bucket in BucketRepository(session).list(active_only=True):
        if bucket.bucket_type is not BucketType.SPECIAL or bucket.special_kind is None:
            continue
        if bucket.quota is not None:
            quotas[bucket.special_kind] = bucket.quota
    return quotas


def _normalize_quotas(
    quotas: Mapping[SpecialBucketKind, int] | None,
) -> dict[SpecialBucketKind, int]:
    result = dict(DEFAULT_SPECIAL_QUOTAS)
    for kind, value in (quotas or {}).items():
        if not isinstance(kind, SpecialBucketKind):
            raise SpecialRotationError("special_rotation.quotas", "键必须是 SpecialBucketKind")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpecialRotationError("special_rotation.quotas", "必须是非负整数")
        result[kind] = value
    return result


def _normalize_used(
    used: Mapping[SpecialBucketKind, int] | None,
) -> dict[SpecialBucketKind, int]:
    result = dict.fromkeys(SPECIAL_ROTATION_ORDER, 0)
    for kind, value in (used or {}).items():
        if not isinstance(kind, SpecialBucketKind):
            raise SpecialRotationError("special_rotation.used", "键必须是 SpecialBucketKind")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpecialRotationError("special_rotation.used", "必须是非负整数")
        result[kind] = value
    return result


__all__ = [
    "SPECIAL_ROTATION_ORDER",
    "RotationSlot",
    "SpecialCandidate",
    "SpecialRotationError",
    "SpecialRotationPlan",
    "load_special_candidates",
    "load_special_quotas",
    "plan_special_rotation",
]
