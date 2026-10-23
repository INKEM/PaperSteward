"""初始化阶段迁移；仅 flush，外部事务决定提交，失败操作自身原子回滚。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from papersteward.core.entities import Event
from papersteward.core.enums import EventType
from papersteward.core.errors import InvariantViolation
from papersteward.core.onboarding import Onboarding, OnboardingStage
from papersteward.core.onboarding import enter_draft as core_enter_draft
from papersteward.storage.models import OnboardingRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import EventRepository


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


def enter_draft(session: Session, *, event_id: str, occurred_at: datetime) -> Onboarding:
    """SETUP → DRAFT；幂等：已进入草案阶段时原样返回。"""
    state = get_onboarding(session)
    if state.stage is OnboardingStage.DRAFT:
        return state
    updated = core_enter_draft(state)
    with _atomic(session):
        row = session.get(OnboardingRow, 1)
        if row is None:
            session.add(
                OnboardingRow(
                    id=1,
                    stage=updated.stage.value,
                    revision=updated.revision,
                    updated_at=occurred_at,
                )
            )
        else:
            if row.stage != state.stage.value:
                raise InvariantViolation("onboarding.stage", "状态已变化，请刷新后重试")
            row.stage = updated.stage.value
            row.revision = updated.revision
            row.updated_at = occurred_at
        EventRepository(session).append(
            Event(
                id=event_id,
                event_type=EventType.ONBOARDING_DRAFT_ENTERED,
                created_at=occurred_at,
                payload_json={"from": state.stage.value, "to": updated.stage.value},
            )
        )
    return updated


def transition_active(
    session: Session, *, expected_revision: int, occurred_at: datetime
) -> Onboarding:
    """DRAFT → ACTIVE 的乐观并发写入；事件与业务校验由调用方负责。"""
    from papersteward.core.onboarding import activate

    state = get_onboarding(session)
    if state.revision != expected_revision:
        raise InvariantViolation("onboarding.revision", "状态已变化，请刷新后重新确认")
    updated = activate(state)
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
    return updated


__all__ = ["_atomic", "enter_draft", "transition_active"]
