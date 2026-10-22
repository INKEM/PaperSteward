from dataclasses import asdict
from datetime import date

from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.dynamic import compute_dynamic


def test_dynamic_neutral_contract():
    config = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")
    result = compute_dynamic(
        primary_tag="T01",
        published_on=None,
        as_of=date(2026, 10, 22),
        shares={"T01": 1},
        completions=[],
        config=config,
    )
    assert asdict(result) == {
        "freshness": 50,
        "hunger": 50,
        "dynamic_score": 50,
        "age_days": None,
        "share": 1,
        "target_share": 1,
        "completed_count": 0,
        "total_completed": 0,
        "backlog_dampened": False,
        "reasons": ("missing_publication_date",),
    }
