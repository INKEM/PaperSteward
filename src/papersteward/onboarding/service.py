"""初始化命令；仅 flush，外部事务决定提交，失败操作自身原子回滚。"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from papersteward.core.entities import Event
from papersteward.core.enums import EventType
from papersteward.core.errors import InvariantViolation
from papersteward.core.onboarding import (
    Gate,
    GateApproval,
    Onboarding,
    OnboardingStage,
    advance,
)
from papersteward.storage.models import OnboardingRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import EventRepository

# 可信业务服务校验产物存在、版本匹配和本门全部条件；失败必须抛异常。
GateValidator = Callable[[Session, Onboarding, Gate, GateApproval], None]


@contextmanager
def _atomic(session: Session) -> Iterator[None]:
    # sqlite3 legacy 模式不会为 SELECT 开启事务。裸 SAVEPOINT 被 release 后可能
    # 提前提交，导致调用方 rollback 无效；先确保实际存在外层事务。
    connection = session.connection()
    raw = connection.connection.dbapi_connection
    if isinstance(raw, sqlite3.Connection) and not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")
    with session.begin_nested():
        yield


def start(session: Session, *, event_id: str, occurred_at: datetime) -> Onboarding:
    state = get_onboarding(session)
    if state.stage is not OnboardingStage.SEEDS:
        return state
    with _atomic(session):
        session.add(OnboardingRow(id=1, stage="profile", revision=1, updated_at=occurred_at))
        EventRepository(session).append(
            Event(
                id=event_id,
                event_type=EventType.ONBOARDING_STARTED,
                created_at=occurred_at,
                payload_json={"from": "seeds", "to": "profile", "revision": 1},
            )
        )
    return Onboarding(OnboardingStage.PROFILE, 1)


def confirm_gate(
    session: Session,
    gate: Gate,
    approval: GateApproval,
    *,
    expected_revision: int,
    validate_evidence: GateValidator,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    """顺序检查 → 可信业务证据检查 → revision 条件写入 → 追加用户确认事件。

    不提供默认放行校验器，也不将确认接口直接暴露给外部请求。
    """
    state = get_onboarding(session)
    if state.revision != expected_revision:
        raise InvariantViolation("onboarding.revision", "状态已变化，请刷新后重新确认")
    updated = advance(state, gate)
    with _atomic(session):
        validate_evidence(session, state, gate, approval)
        changed = session.execute(
            update(OnboardingRow)
            .where(
                OnboardingRow.id == 1,
                OnboardingRow.revision == expected_revision,
                OnboardingRow.stage == state.stage.value,
            )
            .values(stage=updated.stage.value, revision=updated.revision, updated_at=occurred_at)
            .returning(OnboardingRow.id)
        ).scalar_one_or_none()
        if changed is None:
            raise InvariantViolation("onboarding.revision", "状态已变化，请刷新后重新确认")
        EventRepository(session).append(
            Event(
                id=event_id,
                event_type=EventType(f"onboarding.{gate.value}.confirmed"),
                created_at=occurred_at,
                payload_json={
                    **asdict(approval),
                    "gate": gate.value,
                    "from": state.stage.value,
                    "to": updated.stage.value,
                    "revision": updated.revision,
                },
            )
        )
    return updated
