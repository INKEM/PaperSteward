from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from papersteward.scoring import ConfigError, load_scoring_config_file, load_scoring_config_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VALID = FIXTURES / "configs" / "scoring.yaml"


def _data() -> dict[str, Any]:
    data = yaml.safe_load(VALID.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _load(data: dict[str, Any], *, source: str = "memory.yaml") -> Any:
    return load_scoring_config_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), source=source
    )


def test_load_valid_scoring_fixture() -> None:
    config = load_scoring_config_file(VALID)

    assert config.version == "1.0.0"
    assert config.static.weight == pytest.approx(0.70)
    assert config.static.relevance == pytest.approx(0.65)
    assert config.gates.min_relevance_for_push == 55
    assert config.dynamic.half_life_days.default == 180
    assert config.quasi_dynamic.update_weekday == "SUN"


def test_weekday_and_numeric_version_are_normalized() -> None:
    data = _data()
    data["version"] = 1.0
    data["quasi_dynamic"]["update_weekday"] = "sun"

    config = _load(data)

    assert config.version == "1.0"
    assert config.quasi_dynamic.update_weekday == "SUN"


def test_policy_floor_violation_is_field_located() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_scoring_config_file(FIXTURES / "configs" / "scoring_invalid.yaml")

    error = exc_info.value
    assert error.field == "scoring.static.relevance"
    assert error.line is not None
    assert "0.50" in error.message


def test_group_weight_sum_must_be_one() -> None:
    with pytest.raises(ConfigError, match="dynamic 子权重"):
        load_scoring_config_file(FIXTURES / "configs" / "scoring_invalid_sum.yaml")


def test_top_level_weight_sum_must_be_one() -> None:
    data = _data()
    data["static"]["weight"] = 0.60
    data["dynamic"]["weight"] = 0.20
    data["quasi_dynamic"]["weight"] = 0.10

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring"
    assert "全局层权重" in exc_info.value.message


def test_static_authority_cap_is_enforced() -> None:
    data = _data()
    data["static"]["relevance"] = 0.50
    data["static"]["authority"] = 0.30
    data["static"]["extra_value"] = 0.20

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.static.authority"
    assert "0.20" in exc_info.value.message


def test_policy_gate_defaults_cannot_be_relaxed() -> None:
    data = _data()
    data["gates"]["relevance_weight_floor"] = 0.40

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.gates.relevance_weight_floor"


def test_gate_relationships_and_half_life_order() -> None:
    data = _data()
    data["gates"]["deep_cap"] = 4
    data["gates"]["skim_cap"] = 5
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.gates.deep_cap"

    data = _data()
    data["dynamic"]["half_life_days"] = {"default": 90, "fast": 120, "slow": 540}
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.dynamic.half_life_days"


def test_unknown_field_is_rejected() -> None:
    data = _data()
    data["unknown_field"] = True

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.unknown_field"


def test_invalid_scalar_and_missing_field_are_field_located() -> None:
    data = _data()
    data["gates"]["max_daily_push"] = 0
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.gates.max_daily_push"

    data = _data()
    del data["stability"]
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.stability"


def test_yaml_syntax_and_root_type_errors() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_scoring_config_text("version: [1.0.0\n", source="bad.yaml")
    assert "YAML" in exc_info.value.message

    with pytest.raises(ConfigError, match="mapping"):
        load_scoring_config_text("- one\n- two\n", source="bad.yaml")


@pytest.mark.parametrize("version", [None, True, [], ""])
def test_invalid_version_is_field_located(version: object) -> None:
    data = _data()
    data["version"] = version

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.version"


def test_non_string_weekday_is_rejected() -> None:
    data = _data()
    data["quasi_dynamic"]["update_weekday"] = 1

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.quasi_dynamic.update_weekday"


def test_policy_static_weight_floor_is_enforced() -> None:
    data = _data()
    data["static"]["weight"] = 0.50
    data["dynamic"]["weight"] = 0.30
    data["quasi_dynamic"]["weight"] = 0.20

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.static.weight"


def test_gate_floor_and_cap_policy_cannot_be_relaxed() -> None:
    data = _data()
    data["gates"]["static_weight_floor"] = 0.55
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.gates.static_weight_floor"

    data = _data()
    data["gates"]["authority_weight_cap"] = 0.30
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.gates.authority_weight_cap"


def test_actual_weights_must_respect_configurable_gates() -> None:
    data = _data()
    data["gates"]["relevance_weight_floor"] = 0.70
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.static.relevance"

    data = _data()
    data["static"]["weight"] = 0.65
    data["dynamic"]["weight"] = 0.25
    data["quasi_dynamic"]["weight"] = 0.10
    data["gates"]["static_weight_floor"] = 0.70
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.static.weight"

    data = _data()
    data["gates"]["authority_weight_cap"] = 0.15
    with pytest.raises(ConfigError) as exc_info:
        _load(data)
    assert exc_info.value.field == "scoring.static.authority"


def test_per_tag_cap_cannot_exceed_daily_push() -> None:
    data = _data()
    data["gates"]["max_per_primary_tag"] = 8

    with pytest.raises(ConfigError) as exc_info:
        _load(data)

    assert exc_info.value.field == "scoring.gates.max_per_primary_tag"
