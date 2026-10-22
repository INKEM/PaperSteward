from datetime import date, timedelta
from pathlib import Path

import pytest

from papersteward.core.errors import InvariantViolation
from papersteward.delivery.compose import DeliveryHistory, compose_digest
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.contracts import QueueItem, ScoreExplanation

DAY = date(2026, 10, 22)
CONFIG = load_scoring_config_file(Path("tests/fixtures/configs/scoring.yaml"))


def item(key="p", tag="T01", rank=70, r=75, e=50, h=50):
    explanation = ScoreExplanation(
        paper_id=key,
        score_date=DAY,
        config_version="1.0.0",
        profile_version="1.0",
        taxonomy_version="1.0",
        static_input_hash="0" * 64,
        primary_tag=tag,
        components={
            k: {"score": v, "weight": 0.1, "contribution": v / 10, "confidence": 0.5}
            for k, v in {"R": r, "E": e, "H": h, "A": 50}.items()
        },
        layer_weights={},
        scores={"P": rank, "rank_score": rank},
        dynamic={},
        quasi={},
        stability={},
        evidence=[],
        reasons=[],
        binding={},
    )
    return QueueItem(
        paper_id=key,
        title=key,
        primary_tag=tag,
        summary_100="方法与结果",
        published_on=DAY,
        score_date=DAY,
        priority=rank,
        rank_score=rank,
        explanation=explanation,
    )


def compose(items, **kwargs):
    return compose_digest(
        items,
        day=DAY,
        timezone="Asia/Shanghai",
        binding={},
        config=CONFIG,
        weekly_capacity=3,
        **kwargs,
    )


def test_caps_diversity_starved_slot_and_stable_order():
    rows = [
        item(
            str(n),
            "T0" + str(n // 3 + 1),
            rank=90 - n,
            r=85 if n < 2 else 75,
            e=75 if n < 2 else 50,
        )
        for n in range(12)
    ]
    rows.append(item("hungry", "T05", rank=40, h=90))
    result = compose(rows)
    assert len(result.items) == 7
    assert sum(i.read_mode == "deep" for i in result.items) == 2
    assert sum(i.read_mode == "skim" for i in result.items) == 5
    assert "hungry" in [i.paper_id for i in result.items]
    assert (
        max(
            sum(i.primary_tag == tag for i in result.items) for tag in {i.primary_tag for i in rows}
        )
        <= 2
    )
    assert result.inventory_mode and result.special_slots == 2
    assert result == compose(list(reversed(rows)))


@pytest.mark.parametrize("count", [0, 1, 2, 12])
def test_shortage_never_invents_candidates(count):
    result = compose([item(str(n)) for n in range(count)])
    assert len(result.items) == min(count, 2)


def test_deep_overflow_deferred_and_explicit_downgrade():
    rows = [item(str(n), f"T{n}", r=90, e=80) for n in range(4)]
    assert len(compose(rows).items) == 2
    result = compose(rows, allow_downgrade=True)
    assert len(result.items) == 4 and "额度已满" in result.items[-1].reason


def test_cooldowns_resumption_and_repush_threshold():
    p = item()
    old = DeliveryHistory(DAY - timedelta(days=10), 60, "0" * 64)
    assert compose([p], history={"p": old}).items == []  # exactly10不重推
    assert compose([item(rank=70.1)], history={"p": old}).items
    assert (
        compose([p], history={"p": DeliveryHistory(skip_until=DAY + timedelta(days=1))}).items == []
    )
    assert compose([p], history={"p": DeliveryHistory(snooze_until=DAY)}).items
    assert compose([p], history={"p": DeliveryHistory(skip_until=DAY)}).items


def test_invalid_queue_and_config_rejected():
    with pytest.raises(InvariantViolation):
        compose([item(), item()])
    with pytest.raises(InvariantViolation):
        compose([item().model_copy(update={"score_date": DAY - timedelta(days=1)})])
