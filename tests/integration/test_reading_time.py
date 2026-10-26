"""阅读时间声明的领域服务与 JSON 接口验证（R1-02）。

覆盖：追加/更正/清除与版本递增、乐观并发、幂等重放、日期范围、
导入同事务、更正不改评分/事件/快照、跨时区自然日期、重置清空。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_reading_insight_projection import (
    T_DONE,
    import_content,
    seed_archive,
    seed_paper,
    system_content,
)

from conftest import activate_workspace
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.reading.import_service import import_read_paper
from papersteward.reading.insights.contracts import ReadTimeDeclaration
from papersteward.reading.insights.timeline import read_time_view, record_reading_time
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    EventRow,
    PaperRow,
    RatingRow,
    ReadingTimeRevisionRow,
)
from papersteward.storage.workspace import clear_workspace

# 测试固定“今天”：NOW2 在上海时区为 2026-10-26。
NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def day_declaration(day: str) -> ReadTimeDeclaration:
    return ReadTimeDeclaration.model_validate({"precision": "day", "day": day})


def month_declaration(month: str) -> ReadTimeDeclaration:
    return ReadTimeDeclaration.model_validate({"precision": "month", "month": month})


UNKNOWN_DECLARATION = ReadTimeDeclaration.model_validate({"precision": "unknown"})


def seed_import(session, paper_id: str, *, title: str) -> None:
    seed_paper(session, paper_id, first_seen_at=T_DONE)
    seed_archive(session, paper_id, archived_at=T_DONE, content=import_content(title=title))


def test_record_and_revise_and_clear_reading_time(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_import(session, "imp-a", title="导入A")
    with session_scope(factory) as session:
        view = record_reading_time(
            session, "imp-a", day_declaration("2026-10-20"), now=NOW2, expected_revision=0
        )
        assert view.current_revision == 1
        assert view.effective.precision.value == "day"
        assert str(view.effective.day) == "2026-10-20"
        assert view.effective.basis.value == "declared"
    with session_scope(factory) as session:
        view = record_reading_time(
            session, "imp-a", month_declaration("2026-09"), now=NOW2, expected_revision=1
        )
        assert view.current_revision == 2
        assert view.effective.precision.value == "month"
        assert view.effective.month == "2026-09"
        assert [r.revision for r in view.revisions] == [1, 2]
    with session_scope(factory) as session:
        # 显式清除：unknown 也是有效声明。
        view = record_reading_time(
            session, "imp-a", UNKNOWN_DECLARATION, now=NOW2, expected_revision=2
        )
        assert view.current_revision == 3
        assert view.effective.precision.value == "unknown"


def test_expected_revision_conflict_reports_current(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_import(session, "imp-b", title="导入B")
        record_reading_time(
            session, "imp-b", day_declaration("2026-10-01"), now=NOW2, expected_revision=0
        )
    with session_scope(factory) as session, pytest.raises(ConflictError) as excinfo:
        record_reading_time(
            session, "imp-b", day_declaration("2026-10-02"), now=NOW2, expected_revision=0
        )
    assert excinfo.value.details == {"current_revision": 1}


def test_idempotency_key_replays_same_input_only(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_import(session, "imp-c", title="导入C")
        first = record_reading_time(
            session,
            "imp-c",
            day_declaration("2026-10-03"),
            now=NOW2,
            expected_revision=0,
            idempotency_key="key-1",
        )
        replay = record_reading_time(
            session,
            "imp-c",
            day_declaration("2026-10-03"),
            now=NOW2,
            expected_revision=0,
            idempotency_key="key-1",
        )
        assert replay.current_revision == first.current_revision == 1
        with pytest.raises(InvariantViolation):
            record_reading_time(
                session,
                "imp-c",
                day_declaration("2026-10-04"),
                now=NOW2,
                expected_revision=1,
                idempotency_key="key-1",
            )


def test_declaration_not_later_than_workspace_today(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_import(session, "imp-d", title="导入D")
        # NOW2 上海时区为 2026-10-26：次日/次月拒绝，当天/当月/过去合法。
        with pytest.raises(InvalidRequestError):
            record_reading_time(
                session, "imp-d", day_declaration("2026-10-27"), now=NOW2, expected_revision=0
            )
        with pytest.raises(InvalidRequestError):
            record_reading_time(
                session, "imp-d", month_declaration("2026-11"), now=NOW2, expected_revision=0
            )
        record_reading_time(
            session, "imp-d", day_declaration("2026-10-26"), now=NOW2, expected_revision=0
        )
        record_reading_time(
            session, "imp-d", month_declaration("2026-10"), now=NOW2, expected_revision=1
        )
        view = record_reading_time(
            session, "imp-d", UNKNOWN_DECLARATION, now=NOW2, expected_revision=2
        )
        assert view.effective.precision.value == "unknown"


def test_unknown_paper_is_not_found(insights_factory):
    with session_scope(insights_factory) as session, pytest.raises(NotFoundError):
        record_reading_time(
            session, "missing", day_declaration("2026-10-01"), now=NOW2, expected_revision=0
        )


def test_import_with_read_time_is_atomic(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput.model_validate(
                {
                    "title": "带月份的导入",
                    "score": 4,
                    "read_time": {"precision": "month", "month": "2026-09"},
                }
            ),
            now=NOW2,
        )
    with session_scope(factory) as session:
        view = read_time_view(session, paper_id)
        assert view.current_revision == 1
        assert view.effective.month == "2026-09"

    with session_scope(factory) as session, pytest.raises(InvalidRequestError):
        import_read_paper(
            session,
            ImportReadPaperInput.model_validate(
                {
                    "title": "日期非法的导入",
                    "score": 3,
                    "read_time": {"precision": "day", "day": "2027-01-01"},
                }
            ),
            now=NOW2,
        )
    # 同成同败：整包回滚，无任何残留行。
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PaperRow).where(PaperRow.title == "日期非法的导入")
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(ReadingTimeRevisionRow)) == 1


def test_import_without_read_time_stays_compatible(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": "无日期导入", "score": 3}),
            now=NOW2,
        )
    with session_scope(factory) as session:
        view = read_time_view(session, paper_id)
        assert view.current_revision == 0
        assert view.effective.precision.value == "unknown"


def test_correction_does_not_touch_score_channel(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_paper(session, "sys-a", first_seen_at=T_DONE)
        seed_archive(
            session,
            "sys-a",
            archived_at=T_DONE,
            content=system_content(title="系统阅读", completed_at=T_DONE),
            rating_col=5,
            note_col="初始",
            ratings=[(T_DONE, 5, "初始")],
        )
    with session_scope(factory) as session:
        before_hash = session.get(ArchiveSnapshotRow, "sys-a").content_hash
        before_events = session.scalar(select(func.count()).select_from(EventRow))
        before_ratings = session.scalars(select(RatingRow)).all()
        record_reading_time(
            session, "sys-a", day_declaration("2026-10-19"), now=NOW2, expected_revision=0
        )
        assert session.get(ArchiveSnapshotRow, "sys-a").content_hash == before_hash
        assert session.scalar(select(func.count()).select_from(EventRow)) == before_events
        assert session.scalars(select(RatingRow)).all() == before_ratings
        assert session.get(PaperRow, "sys-a").status == "archived"
        view = read_time_view(session, "sys-a")
        assert view.effective.basis.value == "declared"
        assert str(view.effective.day) == "2026-10-19"


def test_cross_timezone_natural_date_is_not_shifted(insights_factory):
    factory = insights_factory
    # 系统完成时刻 2026-10-25T16:30Z → 上海 10-26 00:30，系统默认归 10-26。
    completed = datetime(2026, 10, 25, 16, 30, tzinfo=UTC)
    with session_scope(factory) as session:
        seed_paper(session, "sys-tz", first_seen_at=completed)
        seed_archive(
            session,
            "sys-tz",
            archived_at=completed,
            content=system_content(title="跨时区", completed_at=completed),
            rating_col=4,
        )
    with session_scope(factory) as session:
        view = read_time_view(session, "sys-tz")
        assert str(view.effective.day) == "2026-10-26"
        assert view.effective.basis.value == "system_recorded"
    with session_scope(factory) as session:
        view = record_reading_time(
            session, "sys-tz", day_declaration("2026-10-25"), now=NOW2, expected_revision=0
        )
        # 用户声明的自然日期不做时区平移。
        assert str(view.effective.day) == "2026-10-25"
        assert view.effective.basis.value == "declared"


def test_workspace_reset_clears_revisions(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session:
        seed_import(session, "imp-reset", title="导入R")
        record_reading_time(
            session, "imp-reset", month_declaration("2026-08"), now=NOW2, expected_revision=0
        )
    with session_scope(factory) as session:
        clear_workspace(session, keep_model=False)
        assert session.scalar(select(func.count()).select_from(ReadingTimeRevisionRow)) == 0


def test_json_api_read_time_roundtrip(client, migrated_engine):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": "API导入", "score": 4}),
            now=NOW2,
        )

    response = client.get(f"/reading-insights/papers/{paper_id}/read-time")
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == 0
    assert body["effective"]["precision"] == "unknown"

    created = client.post(
        f"/reading-insights/papers/{paper_id}/read-time",
        json={
            "precision": "day",
            "day": "2026-09-30",
            "expected_revision": 0,
            "idempotency_key": "api-key-1",
        },
    )
    assert created.status_code == 201
    assert created.json()["effective"]["day"] == "2026-09-30"

    conflict = client.post(
        f"/reading-insights/papers/{paper_id}/read-time",
        json={
            "precision": "day",
            "day": "2026-09-29",
            "expected_revision": 0,
            "idempotency_key": "api-key-2",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"] == {"current_revision": 1}

    # 幂等键重放：同输入不产生第二个版本。
    replay = client.post(
        f"/reading-insights/papers/{paper_id}/read-time",
        json={
            "precision": "day",
            "day": "2026-09-30",
            "expected_revision": 0,
            "idempotency_key": "api-key-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["current_revision"] == 1

    strict = client.post(
        f"/reading-insights/papers/{paper_id}/read-time",
        json={"precision": "day", "surprise": 1, "expected_revision": 1, "idempotency_key": "k3"},
    )
    assert strict.status_code == 422

    missing = client.get("/reading-insights/papers/nope/read-time")
    assert missing.status_code == 404
