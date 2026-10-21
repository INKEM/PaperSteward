from dataclasses import asdict

import pytest

from papersteward.core.errors import DomainValidationError, InvalidTransition
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage, advance


def test_all_gates_require_explicit_ordered_confirmation() -> None:
    state = Onboarding(OnboardingStage.PROFILE, 1)
    for gate, target in zip(Gate, list(OnboardingStage)[2:], strict=True):
        previous = state
        state = advance(state, gate)
        assert state.stage is target
        assert state.revision == previous.revision + 1
        assert previous.stage != state.stage
    assert state.next_gate is None


@pytest.mark.parametrize("stage", OnboardingStage)
@pytest.mark.parametrize("gate", Gate)
def test_skipping_repeating_and_confirming_before_start_are_rejected(
    stage: OnboardingStage, gate: Gate
) -> None:
    state = Onboarding(stage)
    if state.next_gate is gate:
        return
    with pytest.raises(InvalidTransition, match="onboarding.stage"):
        advance(state, gate)


@pytest.mark.parametrize(
    "field", ["confirmed_by", "artifact_id", "artifact_version", "change_summary"]
)
def test_confirmation_requires_user_version_and_summary(field: str) -> None:
    values = asdict(GateApproval("user", "artifact", "v1", "已审阅"))
    values[field] = "  "
    with pytest.raises(DomainValidationError, match=field):
        GateApproval(**values)
