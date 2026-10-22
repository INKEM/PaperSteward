from __future__ import annotations

from pathlib import Path

import pytest

from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.governance import (
    apply_overrides,
    component_weights,
    next_config_version,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_scoring_config_file(ROOT / "tests/fixtures/configs/scoring.yaml")


def test_component_weights_expand_layers_to_one() -> None:
    weights = component_weights(CONFIG)
    assert weights["R"] == pytest.approx(0.70 * 0.65)
    assert weights["A"] == pytest.approx(0.70 * 0.20)
    assert weights["E"] == pytest.approx(0.70 * 0.15)
    assert weights["F"] == pytest.approx(0.20 * 0.60)
    assert weights["H"] == pytest.approx(0.20 * 0.40)
    assert weights["U"] == pytest.approx(0.10)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_next_config_version_increments_last_numeric_segment() -> None:
    assert next_config_version("1.0.0") == "1.0.1"
    assert next_config_version("1.0") == "1.1"
    assert next_config_version("abc") == "abc.1"


def test_apply_overrides_bumps_version_and_validates_cross_fields() -> None:
    proposed = apply_overrides(
        CONFIG,
        {"static.relevance": 0.50, "static.extra_value": 0.30},
    )
    assert proposed.version == "1.0.1"
    assert proposed.static.relevance == 0.50
    assert proposed.static.authority == 0.20
    assert proposed.static.extra_value == 0.30
    custom = apply_overrides(CONFIG, {"static.weight": 0.69, "dynamic.weight": 0.21}, version="2.0")
    assert custom.version == "2.0" and custom.dynamic.weight == 0.21
    with pytest.raises((DomainError, AppError)):
        apply_overrides(CONFIG, {"static.relevance": 0.40})
    with pytest.raises((DomainError, AppError)):
        apply_overrides(CONFIG, {"unknown.field": 1})
    with pytest.raises((DomainError, AppError)):
        apply_overrides(CONFIG, {"static.relevance": 0.50}, version="1.0.0")
