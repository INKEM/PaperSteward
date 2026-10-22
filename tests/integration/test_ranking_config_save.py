import pytest

from papersteward.scoring.config import ConfigError, load_scoring_config_file
from papersteward.scoring.service import save_scoring_config
from papersteward.storage.repositories import ConfigRepository, EventRepository


def test_invalid_constructed_config_cannot_write_or_emit(db_session):
    base = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")
    for invalid in (
        base.model_copy(
            update={"dynamic": base.dynamic.model_copy(update={"freshness": float("nan")})}
        ),
        base.model_copy(
            update={"static": base.static.model_copy(update={"relevance": 0.4, "extra_value": 0.4})}
        ),
    ):
        with pytest.raises(ConfigError):
            save_scoring_config(db_session, invalid)
    assert ConfigRepository(db_session).versions("scoring") == []
    assert EventRepository(db_session).recent() == []
