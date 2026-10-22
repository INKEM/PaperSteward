import pytest
from pydantic import ValidationError

from papersteward.api.jobs import WeeklyRequest


@pytest.mark.parametrize("run_id", ["", "x" * 65, "../outside", "line\nbreak"])
def test_weekly_run_id_rejects_unsafe_or_ambiguous_values(run_id):
    with pytest.raises(ValidationError):
        WeeklyRequest(run_id=run_id)
