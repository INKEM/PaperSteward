import pytest
from pydantic import ValidationError

from papersteward.delivery.contracts import Action, Digest


def test_digest_special_placeholder_and_strict_rating():
    assert "special_slots" in Digest.model_json_schema()["properties"]
    for score in (True, 1.5, "4", 0, 6):
        with pytest.raises(ValidationError):
            Action(request_id="r", kind="done", score=score)
