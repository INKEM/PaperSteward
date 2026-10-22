from datetime import date, timedelta

import pytest

from papersteward.core.errors import DomainValidationError
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.preference import Feedback, compute_preference, weekly_cutoff

CONFIG = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")
DAY = date(2026, 10, 22)


def calc(history, **kwargs):
    return compute_preference(
        paper_id="candidate",
        primary_tag="T1",
        secondary_tags=kwargs.pop("secondary_tags", []),
        authors=["author"],
        venue="venue",
        feedback=history,
        as_of=kwargs.pop("as_of", DAY),
        config=CONFIG,
        **kwargs,
    )


def test_cold_start_and_unrated_are_neutral():
    assert calc([]).preference == 50
    result = calc([Feedback("read", date(2026, 10, 1), None, ("T1",))])
    assert result.preference == 50 and result.confidence == 0
    assert "no_ratings" in result.reasons


def test_affinity_shrinkage_and_coverage():
    history = [
        Feedback(str(i), date(2026, 10, 1), 5, ("T1",), ("author",), "venue") for i in range(10)
    ]
    result = calc(history)
    assert result.tag == pytest.approx(83.3333)
    assert result.confidence == 0.5 and result.preference == 66.7
    sparse = history + [Feedback(f"unrated{i}", date(2026, 10, 1), None) for i in range(90)]
    assert calc(sparse).preference == 55.6
    assert "insufficient_rating_coverage" in calc(sparse).reasons


def test_weekly_boundary_and_candidate_exclusion():
    assert weekly_cutoff(DAY) == date(2026, 10, 18)
    history = [
        Feedback("late", date(2026, 10, 18), 5, ("T1",)),
        Feedback("candidate", date(2026, 10, 1), 1, ("T1",)),
    ]
    assert calc(history).preference == 50
    assert calc(history, as_of=DAY + timedelta(days=3)).preference > 50


def test_top_three_secondaries_and_parent_prior():
    history = [Feedback("old", date(2026, 10, 1), 5, ("parent",))]
    result = calc(history, secondary_tags=["new", "x", "y", "ignored"], parents={"new": "parent"})
    assert result.preference > 50
    assert result == calc(
        history, secondary_tags=["new", "x", "y", "different"], parents={"new": "parent"}
    )
    for bad in (0, 6, True, 2.5):
        with pytest.raises(DomainValidationError):
            Feedback("bad", DAY, bad)
    with pytest.raises(DomainValidationError):
        calc(history * 2)
