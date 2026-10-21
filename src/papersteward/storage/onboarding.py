"""初始化状态读取与统一执行门禁；缺少状态时默认关闭。"""

from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.core.onboarding import Onboarding, OnboardingStage
from papersteward.storage.models import OnboardingRow


def get_onboarding(session: Session) -> Onboarding:
    # 查询刷新会话内缓存，避免另一事务已经推进后仍读取旧状态。
    row = session.get(OnboardingRow, 1, populate_existing=True)
    return Onboarding() if row is None else Onboarding(OnboardingStage(row.stage), row.revision)


def ensure_active(session: Session, *, action: str) -> None:
    state = get_onboarding(session)
    if state.stage is not OnboardingStage.ACTIVE:
        raise InvariantViolation(
            "onboarding.g4",
            f"尚未完成 G4，禁止 {action}；当前 {state.stage.value}，请先完成初始化确认",
        )
