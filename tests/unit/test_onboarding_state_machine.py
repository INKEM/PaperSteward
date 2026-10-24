import pytest

from papersteward.core.errors import DomainValidationError, InvalidTransition
from papersteward.core.onboarding import Onboarding, OnboardingStage, activate, enter_draft


def test_stages_start_at_setup_and_advance_once() -> None:
    setup = Onboarding()
    assert setup.stage is OnboardingStage.SETUP
    assert setup.revision == 0
    draft = enter_draft(setup)
    assert draft.stage is OnboardingStage.DRAFT
    assert draft.revision == 1
    active = activate(draft)
    assert active.stage is OnboardingStage.ACTIVE
    assert active.revision == 2


def test_stage_revision_must_match() -> None:
    with pytest.raises(DomainValidationError, match="revision"):
        Onboarding(OnboardingStage.DRAFT, 0)
    with pytest.raises(DomainValidationError, match="revision"):
        Onboarding(OnboardingStage.ACTIVE, 5)


def test_skipping_or_repeating_transitions_is_rejected() -> None:
    with pytest.raises(InvalidTransition, match="onboarding.stage"):
        activate(Onboarding())
    with pytest.raises(InvalidTransition, match="onboarding.stage"):
        enter_draft(Onboarding(OnboardingStage.DRAFT, 1))
    with pytest.raises(InvalidTransition, match="onboarding.stage"):
        activate(Onboarding(OnboardingStage.ACTIVE, 2))
