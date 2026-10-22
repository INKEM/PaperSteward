from __future__ import annotations

from datetime import date

import pytest

from papersteward.core.enums import ScoreTier, SpecialBucketKind
from papersteward.delivery.special import (
    SpecialCandidate,
    SpecialRotationError,
    plan_special_rotation,
)

DAY_REVIEW_FIRST = date(2026, 10, 22)  # ordinal % 3 == 0
DAY_CLASSIC_FIRST = date(2026, 10, 23)
DAY_DISTANT_FIRST = date(2026, 10, 24)


def _candidate(
    paper_id: str,
    kind: SpecialBucketKind,
    tier: ScoreTier = ScoreTier.MEDIUM,
    value: float = 60.0,
) -> SpecialCandidate:
    return SpecialCandidate(
        paper_id=paper_id,
        title=f"Title {paper_id}",
        kind=kind,
        tier=tier,
        value=value,
    )


def _sample() -> list[SpecialCandidate]:
    return [
        _candidate("r1", SpecialBucketKind.REVIEW, ScoreTier.HIGH, 80),
        _candidate("r2", SpecialBucketKind.REVIEW, ScoreTier.MEDIUM, 70),
        _candidate("c1", SpecialBucketKind.CLASSIC, ScoreTier.MEDIUM, 60),
        _candidate("d1", SpecialBucketKind.DISTANT, ScoreTier.HIGH, 90),
    ]


def test_rotation_starts_at_review_and_selects_two_buckets() -> None:
    plan = plan_special_rotation(_sample(), day=DAY_REVIEW_FIRST)
    assert [slot.paper_id for slot in plan.slots] == ["r1", "c1"]
    assert [slot.kind for slot in plan.slots] == [
        SpecialBucketKind.REVIEW,
        SpecialBucketKind.CLASSIC,
    ]
    assert plan.used == {
        SpecialBucketKind.REVIEW: 1,
        SpecialBucketKind.CLASSIC: 1,
        SpecialBucketKind.DISTANT: 0,
    }


def test_rotation_start_shifts_with_day_ordinal() -> None:
    classic_first = plan_special_rotation(_sample(), day=DAY_CLASSIC_FIRST)
    assert [slot.kind for slot in classic_first.slots] == [
        SpecialBucketKind.CLASSIC,
        SpecialBucketKind.DISTANT,
    ]
    distant_first = plan_special_rotation(_sample(), day=DAY_DISTANT_FIRST)
    assert [slot.kind for slot in distant_first.slots] == [
        SpecialBucketKind.DISTANT,
        SpecialBucketKind.REVIEW,
    ]


def test_quota_is_a_cap_not_an_order_to_fill() -> None:
    plan = plan_special_rotation(
        _sample(),
        day=DAY_REVIEW_FIRST,
        used={SpecialBucketKind.REVIEW: 3, SpecialBucketKind.DISTANT: 0},
    )
    assert [slot.kind for slot in plan.slots] == [
        SpecialBucketKind.CLASSIC,
        SpecialBucketKind.DISTANT,
    ]
    assert "quota:review" in plan.skipped

    exhausted = plan_special_rotation(
        _sample(),
        day=DAY_REVIEW_FIRST,
        used={
            SpecialBucketKind.REVIEW: 3,
            SpecialBucketKind.CLASSIC: 1,
            SpecialBucketKind.DISTANT: 2,
        },
    )
    assert exhausted.slots == ()
    assert set(exhausted.skipped) == {"quota:review", "quota:classic", "quota:distant"}


def test_low_tier_and_empty_bucket_are_skipped() -> None:
    candidates = [
        _candidate("low", SpecialBucketKind.REVIEW, ScoreTier.LOW, 40),
        _candidate("c1", SpecialBucketKind.CLASSIC, ScoreTier.MEDIUM, 60),
    ]
    plan = plan_special_rotation(candidates, day=DAY_REVIEW_FIRST)
    assert [slot.paper_id for slot in plan.slots] == ["c1"]
    assert "low_tier:review" in plan.skipped
    assert "empty:distant" in plan.skipped


def test_same_bucket_is_selected_once_and_ties_break_by_paper_id() -> None:
    candidates = [
        _candidate("b", SpecialBucketKind.REVIEW, ScoreTier.HIGH, 90),
        _candidate("a", SpecialBucketKind.REVIEW, ScoreTier.HIGH, 90),
        _candidate("c", SpecialBucketKind.REVIEW, ScoreTier.HIGH, 80),
    ]
    plan = plan_special_rotation(candidates, day=DAY_REVIEW_FIRST)
    assert [slot.paper_id for slot in plan.slots] == ["a"]


def test_high_tier_distant_does_not_jump_the_rotation_order() -> None:
    plan = plan_special_rotation(_sample(), day=DAY_REVIEW_FIRST)
    assert "d1" not in [slot.paper_id for slot in plan.slots]


def test_invalid_rotation_inputs_are_rejected() -> None:
    with pytest.raises(SpecialRotationError, match="max_slots"):
        plan_special_rotation([], day=DAY_REVIEW_FIRST, max_slots=3)
    with pytest.raises(SpecialRotationError, match="date"):
        plan_special_rotation([], day="2026-10-22")  # type: ignore[arg-type]
    with pytest.raises(SpecialRotationError, match="重复"):
        plan_special_rotation(
            [
                _candidate("dup", SpecialBucketKind.REVIEW),
                _candidate("dup", SpecialBucketKind.DISTANT),
            ],
            day=DAY_REVIEW_FIRST,
        )
    with pytest.raises(SpecialRotationError, match="非负整数"):
        plan_special_rotation([], day=DAY_REVIEW_FIRST, quotas={SpecialBucketKind.REVIEW: -1})
    with pytest.raises(SpecialRotationError, match="档位"):
        plan_special_rotation([], day=DAY_REVIEW_FIRST, eligible_tiers=frozenset())
