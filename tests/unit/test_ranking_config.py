from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from papersteward.scoring.config import (
    ConfigError,
    ScoringConfig,
    load_scoring_config_file,
    validate_scoring_config,
)

CONFIG = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")


def test_legacy_config_json_is_unchanged():
    original = yaml.safe_load(Path("tests/fixtures/configs/scoring.yaml").read_text())
    assert CONFIG.model_dump(mode="json") == original
    custom = deepcopy(original)
    custom["dynamic"].update(by_primary_tag={"T01": 120}, backlog_dampener=False)
    parsed = ScoringConfig.model_validate(custom)
    assert validate_scoring_config(parsed).model_dump(mode="json") == custom


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("static", "relevance", float("nan")),
        ("stability", "rank_ema", float("inf")),
        ("dynamic", "prior_strength", -1),
        ("dynamic", "by_primary_tag", {"T01": 0}),
        ("dynamic", "by_primary_tag", {"T01": True}),
    ],
)
def test_constructed_models_revalidated(section, field, value):
    config = CONFIG.model_copy(
        update={section: getattr(CONFIG, section).model_copy(update={field: value})}
    )
    with pytest.raises(ConfigError) as exc:
        validate_scoring_config(config)
    assert f"{section}.{field}" in str(exc.value)
