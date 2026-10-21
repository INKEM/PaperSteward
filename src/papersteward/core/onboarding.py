"""初始化顺序契约；不依赖数据库或 HTTP。"""

from dataclasses import dataclass
from enum import StrEnum

from papersteward.core.errors import DomainValidationError, InvalidTransition


class OnboardingStage(StrEnum):
    SEEDS = "seeds"
    PROFILE = "profile"
    TAXONOMY = "taxonomy"
    CALIBRATION = "calibration"
    DRY_RUN = "dry_run"
    ACTIVE = "active"


class Gate(StrEnum):
    G1 = "g1"
    G2 = "g2"
    G3 = "g3"
    G4 = "g4"


@dataclass(frozen=True, slots=True)
class Onboarding:
    stage: OnboardingStage = OnboardingStage.SEEDS
    revision: int = 0

    @property
    def next_gate(self) -> Gate | None:
        return {
            OnboardingStage.PROFILE: Gate.G1,
            OnboardingStage.TAXONOMY: Gate.G2,
            OnboardingStage.CALIBRATION: Gate.G3,
            OnboardingStage.DRY_RUN: Gate.G4,
        }.get(self.stage)


@dataclass(frozen=True, slots=True)
class GateApproval:
    """用户确认的产物版本；不代表产物业务校验已经通过。"""

    confirmed_by: str
    artifact_id: str
    artifact_version: str
    change_summary: str

    def __post_init__(self) -> None:
        for field in ("confirmed_by", "artifact_id", "artifact_version", "change_summary"):
            if not getattr(self, field).strip():
                raise DomainValidationError(f"onboarding.{field}", "确认字段不能为空")


def advance(state: Onboarding, gate: Gate) -> Onboarding:
    if state.next_gate != gate:
        raise InvalidTransition(
            "onboarding.stage", f"当前 {state.stage.value} 不允许确认 {gate.value}"
        )
    target = {
        Gate.G1: OnboardingStage.TAXONOMY,
        Gate.G2: OnboardingStage.CALIBRATION,
        Gate.G3: OnboardingStage.DRY_RUN,
        Gate.G4: OnboardingStage.ACTIVE,
    }[gate]
    return Onboarding(target, state.revision + 1)
