from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select
from test_priority_queue import DAY, NOW, seed

from conftest import activate
from papersteward.cli.main import main
from papersteward.config import Settings, get_settings
from papersteward.core.enums import EventType, PaperStatus, RunStatus
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.actions import apply_action
from papersteward.delivery.archive import read_archive
from papersteward.delivery.contracts import Action
from papersteward.delivery.service import create_digest, deliver
from papersteward.jobs.preference import refresh_weekly
from papersteward.scoring.queue import _context, _history
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import PaperRow, RatingRow, RunRow, ScoreSnapshotRow
from papersteward.storage.repositories import (
    EventRepository,
    PaperRepository,
    ScoreSnapshotRepository,
)

SUNDAY = NOW + timedelta(days=3)


def feedback_ready(client, engine, count=1):
    activate(client)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        for n in range(count):
            seed(session, f"read-{n}")
            session.get(PaperRow, f"read-{n}").authors_json = ["Original Author"]
            session.get(PaperRow, f"read-{n}").venue = "Original Venue"
        seed(session, "unread")
        create_digest(session, DAY, now=NOW)
        for n in range(count):
            apply_action(
                session,
                f"read-{n}",
                Action(
                    request_id=f"done-{n}",
                    kind="done",
                    mode="skim",
                    score=5 if n == 0 else None,
                    note="PRIVATE-NOTE-DO-NOT-READ",
                ),
                now=NOW,
            )
    return factory


def test_weekly_u_uses_final_rating_before_cutoff_and_does_not_change_archive(
    client, migrated_engine
):
    factory = feedback_ready(client, migrated_engine)
    with session_scope(factory) as session:
        archived = deepcopy(read_archive(session, "read-0").model_dump())
        initial_scores = deepcopy(ScoreSnapshotRepository(session).history("read-0"))
        result = refresh_weekly(session, now=SUNDAY)
        assert result.status is RunStatus.SUCCEEDED
        assert result.stats_json["rated_count"] == result.stats_json["completed_count"] == 1
        assert result.stats_json["coverage"] == 1
        score = next(s for s in result.stats_json["scores"] if s["paper_id"] == "unread")
        assert score["U"] > 50
        assert refresh_weekly(session, now=SUNDAY) == result
        assert read_archive(session, "read-0").model_dump() == archived
        assert ScoreSnapshotRepository(session).history("read-0") == initial_scores
        apply_action(
            session,
            "read-0",
            Action(request_id="revise", kind="score", score=1),
            now=SUNDAY + timedelta(hours=1),
        )
        # 本周任务回执和已冻结日快照不追溯改写；下周才使用新反馈。
        assert refresh_weekly(session, now=SUNDAY + timedelta(days=1)).id == result.id
        next_week = refresh_weekly(session, now=SUNDAY + timedelta(days=7))
        assert (
            next(s for s in next_week.stats_json["scores"] if s["paper_id"] == "unread")["U"] < 50
        )
        assert ScoreSnapshotRepository(session).history("read-0") == initial_scores


def delivered_factory(client, engine, tmp_path):
    """先投递日报再阅读归档，复现真实的 pushed 状态。"""
    activate(client)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        seed(session, "read-0")
        session.get(PaperRow, "read-0").authors_json = ["Original Author"]
        session.get(PaperRow, "read-0").venue = "Original Venue"
        seed(session, "unread")
        create_digest(session, DAY, now=NOW)
    deliver(
        factory,
        DAY,
        now=NOW,
        settings=Settings(_env_file=None),
        out=tmp_path / "delivered.md",
    )
    return factory


def test_weekly_includes_delivered_unread_candidates(client, migrated_engine, tmp_path):
    """F1：日报投递后候选为PUSHED，周任务仍须生成并报告当日U。"""
    factory = delivered_factory(client, migrated_engine, tmp_path)
    with session_scope(factory) as session:
        apply_action(
            session,
            "read-0",
            Action(request_id="done-0", kind="done", mode="skim", score=5),
            now=NOW,
        )
        assert PaperRepository(session).get("read-0").status is PaperStatus.ARCHIVED
        assert PaperRepository(session).get("unread").status is PaperStatus.PUSHED
        archived_scores = deepcopy(ScoreSnapshotRepository(session).history("read-0"))
        result = refresh_weekly(session, now=SUNDAY)
        assert result.status is RunStatus.SUCCEEDED
        scores = {score["paper_id"]: score for score in result.stats_json["scores"]}
        assert "unread" in scores and scores["unread"]["U"] > 50
        assert "read-0" not in scores
        latest = ScoreSnapshotRepository(session).history("unread")[-1]
        assert latest.score_date == SUNDAY.date()
        assert PaperRepository(session).get("unread").status is PaperStatus.PUSHED
        assert ScoreSnapshotRepository(session).history("read-0") == archived_scores


def test_weekly_resumes_due_snoozed_candidate(client, migrated_engine, tmp_path):
    factory = delivered_factory(client, migrated_engine, tmp_path)
    with session_scope(factory) as session:
        apply_action(
            session,
            "unread",
            Action(request_id="later", kind="snooze", days=3),
            now=NOW,
        )
        assert PaperRepository(session).get("unread").status is PaperStatus.SNOOZED
        result = refresh_weekly(session, now=SUNDAY)
        assert result.stats_json["resumed"] == 1
        assert PaperRepository(session).get("unread").status is PaperStatus.QUEUED
        assert any(score["paper_id"] == "unread" for score in result.stats_json["scores"])


def test_weekly_same_day_snapshot_is_not_rewritten(client, migrated_engine):
    """N1：周任务不重写日报已生成的同日快照，报告复用该快照。"""
    factory = feedback_ready(client, migrated_engine)
    with session_scope(factory) as session:
        before = deepcopy(ScoreSnapshotRepository(session).history("unread"))
        result = refresh_weekly(session, now=NOW)
        assert result.stats_json["created"] == 0
        assert result.stats_json["existing"] >= 1
        assert ScoreSnapshotRepository(session).history("unread") == before
        assert any(score["paper_id"] == "unread" for score in result.stats_json["scores"])


def test_low_coverage_is_weak_and_notes_never_selected(client, migrated_engine):
    factory = feedback_ready(client, migrated_engine, count=4)
    statements = []

    def listener(conn, cursor, statement, params, context, many):
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", listener)
    try:
        with session_scope(factory) as session:
            result = refresh_weekly(session, now=SUNDAY)
            assert result.stats_json["coverage"] == 0.25 and result.stats_json["weak_signal"]
            assert "PRIVATE" not in str(result.stats_json)
        rating_queries = [s for s in statements if "FROM rating" in s]
        assert rating_queries and all("rating.note" not in s for s in rating_queries)
        archive_queries = [s for s in statements if "FROM archive_snapshot" in s]
        assert archive_queries and all("archive_snapshot.note" not in s for s in archive_queries)
        assert all("JSON_EXTRACT" in s for s in archive_queries)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", listener)


def test_reading_metadata_used_even_if_current_paper_changes(client, migrated_engine):
    factory = feedback_ready(client, migrated_engine)
    with session_scope(factory) as session:
        row = session.get(PaperRow, "read-0")
        row.authors_json, row.venue = ["Later Author"], "Later Venue"
        session.flush()
        _, feedback = _history(session, _context(session, SUNDAY), SUNDAY.date())
        assert feedback[0].authors == ("Original Author",) and feedback[0].venue == "Original Venue"


def test_invalid_rating_data_reported_without_candidate_snapshot_writes(client, migrated_engine):
    factory = feedback_ready(client, migrated_engine)
    with session_scope(factory) as session:
        row = session.get(RatingRow, ("read-0", NOW.replace(tzinfo=None)))
        if row is None:
            row = session.scalar(select(RatingRow).where(RatingRow.paper_id == "read-0"))
        row.tags_at_read_json = ["T01", "T01"]
        session.flush()
        before = session.scalar(select(func.count()).select_from(ScoreSnapshotRow))
        result = refresh_weekly(session, now=SUNDAY)
        assert result.status is RunStatus.FAILED
        assert result.stats_json["data_checks"]["invalid"][0]["reason"] == "duplicate_reading_tags"
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == before


def test_weekly_audit_failure_rolls_back_and_id_conflict(client, migrated_engine, monkeypatch):
    factory = feedback_ready(client, migrated_engine)
    original = EventRepository.append

    def broken(self, event):
        if event.event_type is EventType.PREFERENCE_UPDATED:
            raise RuntimeError("audit failed")
        return original(self, event)

    with session_scope(factory) as session:
        before = session.scalar(select(func.count()).select_from(ScoreSnapshotRow))
        with monkeypatch.context() as m:
            m.setattr(EventRepository, "append", broken)
            with pytest.raises(RuntimeError):
                refresh_weekly(session, now=SUNDAY)
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == before
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunRow)
                .where(RunRow.run_type == "weekly_preference")
            )
            == 0
        )
        refresh_weekly(session, now=SUNDAY, run_id="fixed")
        with pytest.raises(InvariantViolation):
            refresh_weekly(session, now=SUNDAY + timedelta(days=7), run_id="fixed")


def test_weekly_api_cli_gate_and_cold_start(client, migrated_engine, monkeypatch, capsys):
    assert client.post("/jobs/preference/weekly", json={}).status_code == 409
    activate(client)
    monkeypatch.setattr("papersteward.api.jobs.utc_now", lambda: NOW)
    response = client.post("/jobs/preference/weekly", json={})
    assert response.status_code == 200
    assert response.json()["stats_json"]["rated_count"] == 0
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.jobs.utc_now", lambda: NOW)
    get_settings.cache_clear()
    try:
        assert main(["jobs", "preference-weekly", "--json"]) == 0
        assert '"weak_signal": true' in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
