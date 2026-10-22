from dataclasses import asdict
from datetime import date

from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.preference import compute_preference


def test_preference_has_auditable_weekly_cutoff():
    result = compute_preference(
        paper_id="p",
        primary_tag="T1",
        secondary_tags=[],
        authors=[],
        venue=None,
        feedback=[],
        as_of=date(2026, 10, 22),
        config=load_scoring_config_file("tests/fixtures/configs/scoring.yaml"),
    )
    payload = asdict(result)
    assert payload["cutoff"] == date(2026, 10, 18)
    assert payload["ratings_count"] == payload["confidence"] == 0
    assert payload["preference"] == 50
