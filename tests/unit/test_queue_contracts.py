import pytest
from pydantic import ValidationError

from papersteward.scoring.contracts import Component, Exclusion, QueueItem


def test_exclusion_has_no_priority_or_rank_fields():
    assert Exclusion(paper_id="p", reasons=["special_bucket"]).model_dump() == {
        "paper_id": "p",
        "reasons": ["special_bucket"],
        "excluded_from_priority": True,
    }
    assert "priority" not in Exclusion.model_json_schema()["properties"]
    assert "explanation" in QueueItem.model_json_schema()["properties"]


def test_component_rejects_nonfinite_and_invalid_confidence():
    for changes in ({"score": float("nan")}, {"score": 101}, {"weight": -1}, {"confidence": 2}):
        with pytest.raises(ValidationError):
            Component.model_validate(
                {"score": 50, "weight": 0.2, "contribution": 10, "confidence": 0.5, **changes}
            )
