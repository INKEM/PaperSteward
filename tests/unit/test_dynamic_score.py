from datetime import date, timedelta

import pytest

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.dynamic import Completion, compute_dynamic, freshness, target_shares

DAY = date(2026, 10, 22)


@pytest.mark.parametrize(
    "age,expected", [(0, 100), (90, 72.2), (180, 52.5), (360, 28.8), (720, 10.9), (-1, 100)]
)
def test_freshness(age, expected):
    assert freshness(DAY - timedelta(days=age), DAY) == expected


def test_hunger_and_dampener():
    config = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")
    args = {
        "primary_tag": "T1",
        "published_on": DAY,
        "as_of": DAY,
        "shares": {"T1": 0.2, "T2": 0.8},
        "config": config,
    }
    assert compute_dynamic(**args, completions=[]).hunger == 50
    history = [Completion("T2", DAY) for _ in range(10)]
    assert compute_dynamic(**args, completions=history).hunger == 85.7
    assert compute_dynamic(**args, completions=history, backlog=21).hunger == 67.9
    assert compute_dynamic(**args, completions=history, backlog=20).hunger == 85.7
    assert (
        compute_dynamic(**args, completions=[Completion("T1", DAY)] * 5 + history[:5]).hunger == 0
    )
    assert (
        compute_dynamic(
            **args,
            completions=[
                Completion("T2", DAY - timedelta(days=28)),
                Completion("T2", DAY + timedelta(days=1)),
            ],
        ).hunger
        == 50
    )


def test_mapping_and_failures():
    assert target_shares(["T1", "T2"]) == {"T1": 0.5, "T2": 0.5}
    assert target_shares(
        ["T1", "T2"], {"S1": 0.3, "S2": 0.7}, subdirection_to_tag={"S1": "T1", "S2": "T1"}
    ) == {"T1": 1, "T2": 0}
    for bad in ({"T1": 0.2}, {"T1": float("nan")}, {"X": 1}):
        with pytest.raises(DomainValidationError):
            target_shares(["T1", "T2"], bad)
    with pytest.raises(DomainValidationError):
        freshness(DAY, DAY, 0)
    assert freshness(None, DAY) == 50
