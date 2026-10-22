import pytest
from pydantic import ValidationError

from papersteward.delivery.contracts import Action


@pytest.mark.parametrize(
    "values",
    [
        {"kind": "done"},
        {"kind": "done", "mode": "skim", "score": True},
        {"kind": "skip", "score": 4},
        {"kind": "snooze", "days": 2},
        {"kind": "score"},
        {"kind": "note"},
        {"kind": "reject", "note": " "},
    ],
)
def test_action_fields_and_required_intent(values):
    with pytest.raises(ValidationError):
        Action(request_id="r", **values)


def test_explicit_null_clears_feedback_without_using_zero_rating():
    assert Action(request_id="r", kind="score", score=None).score is None
    assert Action(request_id="r", kind="note", note=None).note is None
