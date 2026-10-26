"""阅读时间声明的追加、并发校验与有效值解析；不改归档/评分/评分通道。

写入语义（阶段计划 §8.1）：预期版本检查 + 短写，由调用方持有事务；
竞态由 ``(paper_id, revision)`` 主键唯一约束兜底。读取侧提供单篇视图
与批量投影查询；无声明时回退归档兼容默认（原归档语义不变）。
"""

import re
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.core.entities import ReadingTimeRevision
from papersteward.core.enums import ReadingTimeBasis, TimePrecision
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.archive import verified_archive_content
from papersteward.reading.insights.contracts import (
    ReadingTime,
    ReadTimeDeclaration,
    ReadTimeRevision,
    ReadTimeView,
)
from papersteward.storage.insights import ReadingTimeRevisionRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ArchiveSnapshotRow, ReadingTimeRevisionRow
from papersteward.storage.onboarding import ensure_active
from papersteward.storage.repositories import ConfigRepository

_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_RUNTIME_KEY = "onboarding_runtime"


def record_reading_time(
    session: Session,
    paper_id: str,
    declaration: ReadTimeDeclaration,
    *,
    now: datetime,
    expected_revision: int | None,
    idempotency_key: str | None = None,
) -> ReadTimeView:
    """追加一条阅读时间声明；返回写入后的完整视图。

    ``expected_revision`` 为 ``None`` 时跳过乐观校验（导入路径，归档刚建立）；
    幂等键命中且输入一致时原样返回现状，不产生新版本。
    """
    ensure_active(session, action="reading_time.record")
    if _snapshot(session, paper_id) is None:
        raise NotFoundError("归档不存在")
    repository = ReadingTimeRevisionRepository(session)
    key = idempotency_key or uuid4().hex
    existing = repository.find_by_idempotency_key(key)
    if existing is not None:
        if not _same_declaration(existing, declaration):
            raise InvariantViolation("reading_time.request_id", "请求ID已用于不同阅读时间")
        return read_time_view(session, paper_id)
    current = _current_revision(session, paper_id)
    if expected_revision is not None and current != expected_revision:
        raise ConflictError(
            "阅读时间已被其他页面更新，请基于最新版本重试",
            details={"current_revision": current},
        )
    _validate_range(session, declaration, now=now)
    revision = current + 1
    repository.add(
        ReadingTimeRevision(
            paper_id=paper_id,
            revision=revision,
            recorded_at=as_utc(now),
            precision=declaration.precision,
            basis=ReadingTimeBasis.DECLARED,
            idempotency_key=key,
            day=declaration.day,
            month=declaration.month,
        )
    )
    return read_time_view(session, paper_id)


def read_time_view(session: Session, paper_id: str) -> ReadTimeView:
    """当前阅读时间视图；无声明时按归档冻结内容给出兼容默认。"""
    revisions = ReadingTimeRevisionRepository(session).list_for_paper(paper_id)
    latest = revisions[-1] if revisions else None
    if latest is not None:
        effective = ReadingTime(
            precision=latest.precision,
            basis=latest.basis,
            day=latest.day,
            month=latest.month,
            recorded_at_utc=latest.recorded_at,
        )
    else:
        effective = _archive_default(session, paper_id)
    return ReadTimeView(
        paper_id=paper_id,
        current_revision=len(revisions),
        effective=effective,
        revisions=[
            ReadTimeRevision(
                revision=item.revision,
                recorded_at=item.recorded_at,
                precision=item.precision,
                basis=item.basis,
                day=item.day,
                month=item.month,
            )
            for item in revisions
        ],
    )


def _archive_default(session: Session, paper_id: str) -> ReadingTime:
    """归档兼容默认：系统完成时间按工作区时区归日，其余为未知。"""
    snapshot = _snapshot(session, paper_id)
    if snapshot is None:
        raise NotFoundError("归档不存在")
    try:
        content, available = verified_archive_content(snapshot.content_json, snapshot.content_hash)
    except InvariantViolation:
        content, available = {}, False
    reading_value = content.get("reading")
    reading = reading_value if isinstance(reading_value, dict) else {}
    completed_at = reading.get("completed_at")
    if available and isinstance(completed_at, str):
        try:
            completed = as_utc(datetime.fromisoformat(completed_at))
        except ValueError:
            completed = None
        if completed is not None:
            return ReadingTime(
                precision=TimePrecision.DAY,
                basis=ReadingTimeBasis.SYSTEM_RECORDED,
                day=completed.astimezone(workspace_timezone(session)).date(),
                recorded_at_utc=completed,
            )
    return ReadingTime(precision=TimePrecision.UNKNOWN, basis=ReadingTimeBasis.UNKNOWN)


def workspace_timezone(session: Session) -> ZoneInfo:
    """工作区报告时区；未完成初始化时抛 409 语义的不变量错误。"""
    runtime = ConfigRepository(session).latest(_RUNTIME_KEY)
    if runtime is None:
        raise InvariantViolation(
            "onboarding.confirm", "尚未完成初始化确认，禁止 reading_time.record"
        )
    return ZoneInfo(runtime.value_json["timezone"])


def _validate_range(session: Session, declaration: ReadTimeDeclaration, *, now: datetime) -> None:
    """用户声明是自然日历值；不得晚于填写时工作区的当前日期/月份。"""
    today = as_utc(now).astimezone(workspace_timezone(session)).date()
    if (
        declaration.precision is TimePrecision.DAY
        and declaration.day is not None
        and declaration.day > today
    ):
        raise InvalidRequestError(
            "阅读日期不能晚于今天",
            details={"field": "day", "today": today.isoformat()},
        )
    if declaration.precision is TimePrecision.MONTH and declaration.month is not None:
        year, month = (int(part) for part in declaration.month.split("-"))
        if (year, month) > (today.year, today.month):
            raise InvalidRequestError(
                "阅读月份不能晚于当前月份",
                details={"field": "month", "current": f"{today.year:04d}-{today.month:02d}"},
            )


def _same_declaration(revision: ReadingTimeRevision, declaration: ReadTimeDeclaration) -> bool:
    return (
        revision.precision is declaration.precision
        and revision.day == declaration.day
        and revision.month == declaration.month
    )


def _snapshot(session: Session, paper_id: str) -> ArchiveSnapshotRow | None:
    return session.get(ArchiveSnapshotRow, paper_id)


def _current_revision(session: Session, paper_id: str) -> int:
    return (
        session.scalar(
            select(func.max(ReadingTimeRevisionRow.revision)).where(
                ReadingTimeRevisionRow.paper_id == paper_id
            )
        )
        or 0
    )
