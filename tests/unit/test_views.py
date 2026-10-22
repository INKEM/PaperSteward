from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from papersteward.core.entities import ViewConfig
from papersteward.core.enums import VIEW_COMPONENT_ORDER
from papersteward.errors import InvalidRequestError
from papersteward.scoring.views import (
    PRESET_ORDER,
    PRESETS,
    formula,
    list_presets,
    normalize_view_weights,
    view_score,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_presets_match_design_weights_and_normalize() -> None:
    assert PRESET_ORDER == ("balanced", "frontier", "beginner", "reproducible")
    expected = {
        "balanced": {"R": 45, "A": 15, "E": 10, "F": 12, "H": 8, "U": 10},
        "frontier": {"R": 35, "A": 10, "E": 10, "F": 25, "H": 10, "U": 10},
        "beginner": {"R": 45, "A": 25, "E": 10, "F": 10, "H": 5, "U": 5},
        "reproducible": {"R": 35, "A": 10, "E": 40, "F": 10, "H": 5, "U": 0},
    }
    for name, weights in expected.items():
        preset = PRESETS[name]
        assert preset.weights == weights
        assert math.isclose(sum(preset.normalized.values()), 1.0, abs_tol=1e-9)
        assert set(preset.normalized) == set(VIEW_COMPONENT_ORDER)
    assert [item.name for item in list_presets()] == list(PRESET_ORDER)
    assert "*E" in PRESETS["reproducible"].as_json()["formula"]


def test_normalize_supports_subset_case_and_zero() -> None:
    weights = normalize_view_weights({"r": 35, "E": 40, "F": 15, "A": 10})
    assert weights["R"] == pytest.approx(0.35)
    assert weights["E"] == pytest.approx(0.40)
    assert weights["F"] == pytest.approx(0.15)
    assert weights["A"] == pytest.approx(0.10)
    assert weights["H"] == 0
    assert weights["U"] == 0
    assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)


def test_normalize_rejects_empty_zero_unknown_and_negative() -> None:
    with pytest.raises(InvalidRequestError, match="非空"):
        normalize_view_weights({})
    with pytest.raises(InvalidRequestError, match="至少"):
        normalize_view_weights({"R": 0, "E": 0})
    with pytest.raises(InvalidRequestError, match="未知"):
        normalize_view_weights({"X": 1})
    with pytest.raises(InvalidRequestError, match="0–100"):
        normalize_view_weights({"R": -1})
    with pytest.raises(InvalidRequestError, match="有限"):
        normalize_view_weights({"R": float("nan")})


def test_formula_omits_zero_weights_and_uses_two_decimals() -> None:
    weights = normalize_view_weights({"R": 35, "E": 40, "F": 15, "A": 10})
    assert formula(weights) == "P_view = 0.35*R + 0.10*A + 0.40*E + 0.15*F"


def test_view_score_is_weighted_and_rounded() -> None:
    weights = normalize_view_weights({"R": 40, "A": 10, "E": 20, "F": 10, "H": 10, "U": 10})
    scores = {"R": 80, "A": 60, "E": 50, "F": 40, "H": 70, "U": 30}
    # 80*.4 + 60*.1 + 50*.2 + 40*.1 + 70*.1 + 30*.1 = 62.0
    assert view_score(scores, weights) == 62.0
    with pytest.raises(InvalidRequestError, match="R/A/E/F/H/U"):
        view_score({"R": 50}, weights)


def test_view_config_requires_complete_normalized_weights() -> None:
    good = normalize_view_weights({"R": 50, "E": 50})
    config = ViewConfig(
        id="v1",
        name="my-view",
        weights=good,
        created_at=NOW,
        preset=None,
        description="test",
    )
    assert config.weights["R"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        ViewConfig(
            id="v2",
            name="bad",
            weights={"R": 1.0},
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="权重"):
        ViewConfig(
            id="v3",
            name="bad",
            weights={"R": 1.0, "A": 0, "E": 0, "F": 0, "H": 0, "U": 1.0},
            created_at=NOW,
        )
