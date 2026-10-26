"""结构化阅读理由的领域服务验证（R2-01）。

覆盖：追加/修订/清除与版本递增、乐观并发、幂等重放、键冲突边界、
关联具体评分版本且不随评分修订漂移、空文本归一、论文删除级联。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from test_reading_insight_projection import T_DONE, seed_archive, seed_paper, system_content

from conftest import activate_workspace
from papersteward.core.entities import Rating
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.insights.contracts import StructuredFeedbackInput
from papersteward.reading.insights.structured_feedback import (
    revise_structured_feedback,
    structured_feedback_view,
)
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.models import (
    PaperRow,
    ReadingStructuredFeedbackRow,
    ReadingWriteReceiptRow,
)
from papersteward.storage.repositories import RatingRepository
from papersteward.storage.tx import write_tx

NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def sf_input(
    *,
    expected_revision: int,
    idempotency_key: str | None = None,
    rating_at: datetime | None = None,
    value_sources: tuple[str, ...] = (),
    limited_reasons: tuple[str, ...] = (),
    context_note: str | None = None,
    applicability: str | None = None,
    takeaway: str | None = None,
    question: str | None = None,
    usage_result: str | None = None,
) -> StructuredFeedbackInput:
    return StructuredFeedbackInput.model_validate(
        {
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key or uuid4().hex,
            "rating_at": rating_at,
            "value_sources": list(value_sources),
            "limited_reasons": list(limited_reasons),
            "context_note": context_note,
            "applicability": applicability,
            "takeaway": takeaway,
            "question": question,
            "usage_result": usage_result,
        }
    )


def seed_archived(session, paper_id: str, *, ratings: tuple = ()) -> None:
    seed_paper(session, paper_id, first_seen_at=T_DONE)
    seed_archive(
        session,
        paper_id,
        archived_at=T_DONE,
        content=system_content(title=f"结构化-{paper_id}", completed_at=T_DONE),
        ratings=list(ratings),
    )


def test_revise_and_clear_and_replay(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-a")
    with session_scope(factory) as session:
        view = revise_structured_feedback(
            session,
            "sf-a",
            sf_input(
                expected_revision=0,
                idempotency_key="sf-a-1",
                value_sources=("method_reusable", "aids_understanding"),
                takeaway="学到了评估协议",
            ),
            now=NOW2,
        )
        assert view.current_revision == 1
        assert view.latest is not None
        assert view.latest.value_sources == ["aids_understanding", "method_reusable"]
        assert view.latest.takeaway == "学到了评估协议"
    with session_scope(factory) as session:
        view = revise_structured_feedback(
            session,
            "sf-a",
            sf_input(
                expected_revision=1,
                idempotency_key="sf-a-2",
                value_sources=("method_reusable",),
                limited_reasons=("task_mismatch",),
            ),
            now=NOW2,
        )
        assert view.current_revision == 2
        assert view.latest is not None
        assert view.latest.limited_reasons == ["task_mismatch"]
        assert [item.revision for item in view.history] == [1, 2]
    with session_scope(factory) as session:
        # 显式清除：全空修订是有效状态，历史保留。
        view = revise_structured_feedback(
            session, "sf-a", sf_input(expected_revision=2, idempotency_key="sf-a-3"), now=NOW2
        )
        assert view.current_revision == 3
        assert view.latest is not None
        assert view.latest.value_sources == []
        assert view.latest.takeaway is None
    with session_scope(factory) as session:
        # 回执已保存原结果：同键同请求重放返回当时视图。
        replay = revise_structured_feedback(
            session, "sf-a", sf_input(expected_revision=2, idempotency_key="sf-a-3"), now=NOW2
        )
        assert replay.current_revision == 3
        assert replay.latest is not None and replay.latest.value_sources == []
        assert session.scalar(select(func.count()).select_from(ReadingStructuredFeedbackRow)) == 3


def test_receipt_result_persisted_for_replay(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-r")
        revise_structured_feedback(
            session,
            "sf-r",
            sf_input(expected_revision=0, idempotency_key="sf-r-1", takeaway="收获"),
            now=NOW2,
        )
        receipt = session.scalars(
            select(ReadingWriteReceiptRow).where(ReadingWriteReceiptRow.idempotency_key == "sf-r-1")
        ).one()
        assert receipt.operation == "structured_feedback.revise"
        assert receipt.target_id == "sf-r"
        assert receipt.request_hash is not None
        assert receipt.result_json is not None
        assert receipt.result_json["current_revision"] == 1


def test_same_key_different_payload_conflicts(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-b")
        revise_structured_feedback(
            session,
            "sf-b",
            sf_input(expected_revision=0, idempotency_key="sf-b-key", takeaway="v1"),
            now=NOW2,
        )
        with pytest.raises(InvariantViolation):
            revise_structured_feedback(
                session,
                "sf-b",
                sf_input(expected_revision=1, idempotency_key="sf-b-key", takeaway="v2"),
                now=NOW2,
            )
        assert structured_feedback_view(session, "sf-b").current_revision == 1


def test_same_key_on_other_paper_conflicts_without_writing(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-x")
        seed_archived(session, "sf-y")
        revise_structured_feedback(
            session,
            "sf-x",
            sf_input(expected_revision=0, idempotency_key="cross-key", takeaway="x"),
            now=NOW2,
        )
        with pytest.raises(InvariantViolation):
            revise_structured_feedback(
                session,
                "sf-y",
                sf_input(expected_revision=0, idempotency_key="cross-key", takeaway="y"),
                now=NOW2,
            )
        assert structured_feedback_view(session, "sf-y").current_revision == 0


def test_expected_revision_conflict_reports_current_and_keeps_key(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-c")
        revise_structured_feedback(
            session,
            "sf-c",
            sf_input(expected_revision=0, idempotency_key="sf-c-1", takeaway="第一版"),
            now=NOW2,
        )
    with session_scope(factory) as session, pytest.raises(ConflictError) as excinfo:
        revise_structured_feedback(
            session,
            "sf-c",
            sf_input(expected_revision=0, idempotency_key="sf-c-2", takeaway="过期前提"),
            now=NOW2,
        )
    assert excinfo.value.details == {"current_revision": 1}
    with session_scope(factory) as session:
        # 修正并发前提后同键重试成功：冲突路径不消耗键。
        view = revise_structured_feedback(
            session,
            "sf-c",
            sf_input(expected_revision=1, idempotency_key="sf-c-2", takeaway="过期前提"),
            now=NOW2,
        )
        assert view.current_revision == 2


def test_orphan_key_without_receipt_conflicts(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-o")
        revise_structured_feedback(
            session,
            "sf-o",
            sf_input(expected_revision=0, idempotency_key="sf-orphan", takeaway="有回执"),
            now=NOW2,
        )
        session.execute(
            sa_delete(ReadingWriteReceiptRow).where(
                ReadingWriteReceiptRow.idempotency_key == "sf-orphan"
            )
        )
    with session_scope(factory) as session, pytest.raises(InvariantViolation):
        revise_structured_feedback(
            session,
            "sf-o",
            sf_input(expected_revision=1, idempotency_key="sf-orphan", takeaway="有回执"),
            now=NOW2,
        )


def test_missing_rating_version_rejected_without_consuming_key(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-d")
        with pytest.raises(InvalidRequestError):
            revise_structured_feedback(
                session,
                "sf-d",
                sf_input(
                    expected_revision=0,
                    idempotency_key="sf-d-1",
                    rating_at=datetime(2026, 10, 1, tzinfo=UTC),
                    value_sources=("aids_understanding",),
                ),
                now=NOW2,
            )
        assert structured_feedback_view(session, "sf-d").current_revision == 0
        # 同键可再次使用：确定性校验失败不落回执。
        view = revise_structured_feedback(
            session,
            "sf-d",
            sf_input(expected_revision=0, idempotency_key="sf-d-1", takeaway="有效"),
            now=NOW2,
        )
        assert view.current_revision == 1


def test_rating_link_survives_later_rating_revision(insights_factory):
    """关联具体评分版本：后来追加评分不把旧理由解释成新评分的原因。"""
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-e", ratings=((T_DONE, 4, "初读"),))
    with session_scope(factory) as session:
        view = revise_structured_feedback(
            session,
            "sf-e",
            sf_input(
                expected_revision=0,
                idempotency_key="sf-e-1",
                rating_at=T_DONE,
                value_sources=("provides_arguments",),
            ),
            now=NOW2,
        )
        assert view.latest is not None
        assert view.latest.rating_exists is True
        assert view.latest.rating_score == 4
    with session_scope(factory) as session:
        RatingRepository(session).add(
            Rating("sf-e", 2, "改判", (), None, datetime(2026, 10, 24, 3, tzinfo=UTC))
        )
    with session_scope(factory) as session:
        detail = structured_feedback_view(session, "sf-e").latest
        assert detail is not None
        assert detail.rating_at == T_DONE
        assert detail.rating_score == 4
        assert detail.rating_has_note is True


def test_empty_text_normalizes_to_unset(insights_factory):
    """空理由不补成中立：空白文本按未填写存储，不产生任何维度标记。"""
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-f")
        view = revise_structured_feedback(
            session,
            "sf-f",
            sf_input(
                expected_revision=0,
                idempotency_key="sf-f-1",
                context_note="   ",
                takeaway="",
            ),
            now=NOW2,
        )
        assert view.latest is not None
        assert view.latest.context_note is None
        assert view.latest.takeaway is None
        assert view.latest.value_sources == []
        assert view.latest.limited_reasons == []


def test_unknown_paper_is_not_found(insights_factory):
    with session_scope(insights_factory) as session, pytest.raises(NotFoundError):
        revise_structured_feedback(session, "missing", sf_input(expected_revision=0), now=NOW2)


def test_paper_delete_cascades_structured_rows(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_archived(session, "sf-g")
        revise_structured_feedback(
            session,
            "sf-g",
            sf_input(expected_revision=0, idempotency_key="sf-g-1", takeaway="删除前"),
            now=NOW2,
        )
    with session_scope(factory) as session:
        session.execute(sa_delete(PaperRow).where(PaperRow.id == "sf-g"))
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ReadingStructuredFeedbackRow)) == 0


def test_concurrent_same_key_serializes_and_second_replays(client, migrated_engine):
    """真实双会话并发：第二会话等第一把写锁，提交后命中重放而非重复创建。"""
    import threading

    activate_workspace(client.app.state.engine)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        seed_archived(session, "sf-race")
    lock_held = threading.Event()
    release = threading.Event()
    outcome: dict[str, int] = {}

    def writer_first() -> None:
        session = factory()
        try:
            with write_tx(session):
                view = revise_structured_feedback(
                    session,
                    "sf-race",
                    sf_input(expected_revision=0, idempotency_key="race-key", takeaway="并发"),
                    now=NOW2,
                )
                outcome["first"] = view.current_revision
                lock_held.set()
                assert release.wait(timeout=5)
        finally:
            session.close()

    def writer_second() -> None:
        session = factory()
        try:
            with write_tx(session):
                view = revise_structured_feedback(
                    session,
                    "sf-race",
                    sf_input(expected_revision=0, idempotency_key="race-key", takeaway="并发"),
                    now=NOW2,
                )
                outcome["second"] = view.current_revision
        finally:
            session.close()

    first = threading.Thread(target=writer_first)
    second = threading.Thread(target=writer_second)
    first.start()
    assert lock_held.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not second.is_alive()
    assert outcome == {"first": 1, "second": 1}
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReadingStructuredFeedbackRow)
                .where(ReadingStructuredFeedbackRow.paper_id == "sf-race")
            )
            == 1
        )
