"""初始化顺序契约；不依赖数据库或 HTTP。

新流程：SETUP（连接模型）→ DRAFT（上传材料、Agent 生成草案、反馈迭代）→
ACTIVE（确认后开始使用）。revision 记录阶段迁移次数：setup=0、draft=1、active=2。
"""

from dataclasses import dataclass
from enum import StrEnum

from papersteward.core.errors import DomainValidationError, InvalidTransition


class OnboardingStage(StrEnum):
    SETUP = "setup"
    DRAFT = "draft"
    ACTIVE = "active"


_STAGE_REVISION = {
    OnboardingStage.SETUP: 0,
    OnboardingStage.DRAFT: 1,
    OnboardingStage.ACTIVE: 2,
}


@dataclass(frozen=True, slots=True)
class Onboarding:
    stage: OnboardingStage = OnboardingStage.SETUP
    revision: int = 0

    def __post_init__(self) -> None:
        expected = _STAGE_REVISION[self.stage]
        if self.revision != expected:
            raise DomainValidationError(
                "onboarding.revision",
                f"{self.stage.value} 阶段的 revision 应为 {expected}，实际 {self.revision}",
            )

    @property
    def entered_draft(self) -> bool:
        return self.stage is not OnboardingStage.SETUP


def enter_draft(state: Onboarding) -> Onboarding:
    if state.stage is not OnboardingStage.SETUP:
        raise InvalidTransition(
            "onboarding.stage", f"当前 {state.stage.value} 不再需要进入草案阶段"
        )
    return Onboarding(OnboardingStage.DRAFT, state.revision + 1)


def activate(state: Onboarding) -> Onboarding:
    if state.stage is not OnboardingStage.DRAFT:
        raise InvalidTransition("onboarding.stage", f"当前 {state.stage.value} 不允许确认完成")
    return Onboarding(OnboardingStage.ACTIVE, state.revision + 1)


__all__ = ["Onboarding", "OnboardingStage", "activate", "enter_draft"]
