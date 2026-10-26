"""结构化阅读理由的追加、并发校验与视图；不改评分/备注/归档通道。

写入语义与阅读时间一致（阶段计划 §5）：确定性校验先于回执，预期版本检查
+ 短写，由调用方持有事务；竞态由 ``(paper_id, revision)`` 主键与幂等键唯一
索引兜底。自由文本只存本表与属主视图，不进投影、报告或任何模型通道。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.insights.contracts import (
    STRUCTURED_FEEDBACK_VOCABULARY,
    StructuredFeedbackInput,
    StructuredFeedbackRevisionDetail,
    StructuredFeedbackView,
)
from papersteward.reading.insights.idempotency import (
    STRUCTURED_FEEDBACK_REVISE,
    ReplayResult,
    complete_keyed_write,
    discard_keyed_write,
    open_keyed_write,
    structured_feedback_payload,
)
from papersteward.storage.insights import ReadingStructuredFeedbackRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    RatingRow,
    ReadingStructuredFeedbackRow,
)
from papersteward.storage.onboarding import ensure_active


def revise_structured_feedback(
    session: Session,
    paper_id: str,
    data: StructuredFeedbackInput,
    *,
    now: datetime,
) -> StructuredFeedbackView:
    """追加一条结构化理由；同键同请求重放原结果，不同请求明确冲突。

    回执先于版本检查：响应丢失后的原样重试（expected_revision 已过期）命中
    重放而非误报冲突；回执与版本行同事务提交，任何失败回滚后键不被消耗。
    """
    ensure_active(session, action="structured_feedback.revise")
    if session.get(ArchiveSnapshotRow, paper_id) is None:
        raise NotFoundError("归档不存在")
    # 确定性校验先于回执：非法输入不消耗幂等键。
    rating_at = as_utc(data.rating_at) if data.rating_at is not None else None
    if rating_at is not None and _rating_at(session, paper_id, rating_at) is None:
        raise InvalidRequestError(
            "关联的评分版本不存在，请刷新页面后重新选择",
            details={"field": "rating_at"},
        )
    payload = structured_feedback_payload(
        paper_id=paper_id,
        rating_at=rating_at.isoformat() if rating_at is not None else None,
        value_sources=data.value_sources,
        limited_reasons=data.limited_reasons,
        context_note=_clean_text(data.context_note),
        applicability=_clean_text(data.applicability),
        takeaway=_clean_text(data.takeaway),
        question=_clean_text(data.question),
        usage_result=_clean_text(data.usage_result),
    )
    outcome = open_keyed_write(
        session,
        operation=STRUCTURED_FEEDBACK_REVISE,
        target_id=paper_id,
        key=data.idempotency_key,
        payload=payload,
        now=as_utc(now),
    )
    if isinstance(outcome, ReplayResult):
        if outcome.result is not None:
            return StructuredFeedbackView.model_validate(outcome.result)
        return structured_feedback_view(session, paper_id)
    current = _current_revision(session, paper_id)
    if data.expected_revision != current:
        # 版本冲突可修正后重试：显式移除回执，不依赖调用方回滚也不消耗键。
        discard_keyed_write(outcome)
        raise ConflictError(
            "结构化理由已被其他页面更新，请基于最新版本重试",
            details={"current_revision": current},
        )
    try:
        ReadingStructuredFeedbackRepository(session).add(
            ReadingStructuredFeedbackRow(
                paper_id=paper_id,
                revision=current + 1,
                recorded_at=as_utc(now),
                rating_at=rating_at,
                value_sources_json=list(payload["value_sources"]),
                limited_reasons_json=list(payload["limited_reasons"]),
                context_note=payload["context_note"],
                applicability=payload["applicability"],
                takeaway=payload["takeaway"],
                question=payload["question"],
                usage_result=payload["usage_result"],
                vocabulary_version=STRUCTURED_FEEDBACK_VOCABULARY,
                idempotency_key=data.idempotency_key,
            )
        )
    except IntegrityError:
        # 版本行已有此键但回执缺失（无法证明原请求内容），按冲突处理；
        # 先回滚解除会话失败状态，本次请求的回执与写入一并放弃。
        session.rollback()
        raise InvariantViolation(
            "structured_feedback.request_id", "请求ID已用于不同结构化理由，请更换后重试"
        ) from None
    view = structured_feedback_view(session, paper_id)
    complete_keyed_write(outcome, result=view.model_dump(mode="json"))
    return view


def structured_feedback_view(session: Session, paper_id: str) -> StructuredFeedbackView:
    """当前结构化理由视图；归档不存在按 404 语义，从未填写时 latest 为空。"""
    if session.get(ArchiveSnapshotRow, paper_id) is None:
        raise NotFoundError("归档不存在")
    rows = ReadingStructuredFeedbackRepository(session).list_for_paper(paper_id)
    if not rows:
        return StructuredFeedbackView(paper_id=paper_id, current_revision=0)
    ratings = _ratings_by_time(session, paper_id)
    history = [_detail_from_row(row, ratings) for row in rows]
    return StructuredFeedbackView(
        paper_id=paper_id,
        current_revision=rows[-1].revision,
        latest=history[-1],
        history=history,
    )


def _clean_text(value: str | None) -> str | None:
    """空白串按未填写处理；清除语义由全空修订显式表达。"""
    return value if value else None


def _ratings_by_time(session: Session, paper_id: str) -> dict[datetime, RatingRow]:
    return {
        as_utc(row.created_at): row
        for row in session.scalars(select(RatingRow).where(RatingRow.paper_id == paper_id))
    }


def _rating_at(session: Session, paper_id: str, rating_at: datetime) -> RatingRow | None:
    for row in session.scalars(select(RatingRow).where(RatingRow.paper_id == paper_id)):
        if as_utc(row.created_at) == rating_at:
            return row
    return None


def _detail_from_row(
    row: ReadingStructuredFeedbackRow, ratings: dict[datetime, RatingRow]
) -> StructuredFeedbackRevisionDetail:
    rating = ratings.get(as_utc(row.rating_at)) if row.rating_at is not None else None
    return StructuredFeedbackRevisionDetail(
        revision=row.revision,
        recorded_at=as_utc(row.recorded_at),
        rating_at=as_utc(row.rating_at) if row.rating_at is not None else None,
        rating_exists=rating is not None,
        rating_score=rating.score if rating is not None else None,
        rating_has_note=bool(rating is not None and rating.note),
        value_sources=list(row.value_sources_json or []),
        limited_reasons=list(row.limited_reasons_json or []),
        context_note=row.context_note,
        applicability=row.applicability,
        takeaway=row.takeaway,
        question=row.question,
        usage_result=row.usage_result,
        vocabulary_version=row.vocabulary_version,
    )


def _current_revision(session: Session, paper_id: str) -> int:
    rows = ReadingStructuredFeedbackRepository(session).list_for_paper(paper_id)
    return rows[-1].revision if rows else 0
