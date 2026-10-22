import pytest
from pydantic import ValidationError

from papersteward.scoring.priority import RankConfig


def test_rank_config_boundary():
    assert RankConfig().model_dump()["rank_ema"] == 0.7
    for payload in (
        {"rank_ema": 1.1},
        {"max_daily_delta": -1},
        {"rank_ema": float("nan")},
        {"tie_break": ["unknown"]},
    ):
        with pytest.raises(ValidationError):
            RankConfig.model_validate(payload)
