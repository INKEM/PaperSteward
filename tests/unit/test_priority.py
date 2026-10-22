from datetime import date

import pytest

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.priority import component_priority, compute_priority, rank_key

CONFIG = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")


def test_design_example_and_exact_layered_weights():
    assert (
        component_priority(
            {"R": 84, "A": 72, "E": 65, "F": 80, "H": 55, "U": 60},
            {"R": 0.45, "A": 0.15, "E": 0.10, "F": 0.12, "H": 0.08, "U": 0.10},
        )
        == 75.1
    )
    assert compute_priority(78.8, 70, 60, CONFIG).priority == 75.2
    # P4冻结S至一位小数；使用未舍入S=78.75时得到75.1。
    assert compute_priority(84 * 0.65 + 72 * 0.2 + 65 * 0.15, 70, 60, CONFIG).priority == 75.1


def test_ema_clamp_and_reset():
    result = compute_priority(100, 100, 100, CONFIG, previous_rank=50)
    assert (result.rank_score, result.raw_ema, result.delta_limited) == (58, 85, True)
    assert compute_priority(0, 0, 0, CONFIG, previous_rank=50).rank_score == 42
    assert compute_priority(100, 100, 100, CONFIG, previous_rank=50, reset=True).rank_score == 100
    assert compute_priority(60, 60, 60, CONFIG, previous_rank=50).rank_score == 57
    with pytest.raises(DomainValidationError):
        compute_priority(float("nan"), 50, 50, CONFIG)
    with pytest.raises(DomainValidationError):
        component_priority({"S": 50}, {"S": 0.9})


def test_stable_tie_break():
    base = {
        "rank_score": 70,
        "relevance": 80,
        "published_on": date(2026, 1, 1),
        "authority": 50,
        "title": "Title",
        "paper_id": "a",
    }
    variants = [
        dict(base, relevance=90),
        dict(base, published_on=date(2026, 2, 1)),
        dict(base, authority=60),
        dict(base, title="Alpha"),
        base,
        dict(base, paper_id="b"),
        dict(base, published_on=None),
    ]
    expected = list(variants)
    variants.reverse()
    assert sorted(variants, key=lambda x: rank_key(**x)) == expected
